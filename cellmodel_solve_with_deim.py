import math
from timeit import default_timer as timer
import random
import resource
import sys
import numpy as np
from statistics import mean

from hapod.cellmodel.wrapper import (
    CellModelSolver,
    DuneCellModel,
    CellModel,
    CellModel,
    CellModelReductor,
)
from hapod.mpi import MPIWrapper
from hapod.boltzmann.utility import solver_statistics
from hapod.hapod import (
    local_pod,
    HapodParameters,
    binary_tree_hapod_over_ranks,
    binary_tree_depth,
)
import cProfile

# set_log_levels({'pymor.algorithms.newton': 'WARN'})


class BinaryTreeHapodResults:
    def __init__(self, tree_depth, epsilon_ast, omega):
        self.params = HapodParameters(tree_depth, epsilon_ast=epsilon_ast, omega=omega)
        self.modes = None
        self.svals = None
        self.max_local_modes = 0
        self.max_vectors_before_pod = 0
        self.total_num_snapshots = 0


# Performs a POD on each processor core with the data vectors computed on that core,
# then sends resulting (scaled) modes to rank 0 on that processor.
# The resulting modes are stored in results.gathered_modes on processor rank 0
# results.num_snaps (on rank 0) contains the total number of snapshots
# that have been processed (on all ranks)
def pods_on_processor_cores_in_binary_tree_hapod(r, vecs):
    print("start processor pod: ", mpi.rank_world)
    r.max_vectors_before_pod = max(r.max_vectors_before_pod, len(vecs))
    vecs, svals = local_pod([vecs], len(vecs), r.params, incremental_gramian=False, orth_tol=r.orth_tol,)
    r.max_local_modes = max(r.max_local_modes, len(vecs))
    vecs.scal(svals)
    r.gathered_modes, _, r.num_snaps, _ = mpi.comm_proc.gather_on_rank_0(vecs, len(vecs), num_modes_equal=False)


# perform a POD with gathered modes on rank 0 on each processor/node
def pod_on_node_in_binary_tree_hapod(r, chunk_index, num_chunks, mpi):
    print("start node pod: ", mpi.rank_world)
    r.total_num_snapshots += r.num_snaps
    if chunk_index == 0:
        r.max_vectors_before_pod = max(r.max_vectors_before_pod, len(r.gathered_modes))
        r.modes, r.svals = local_pod([r.gathered_modes], r.num_snaps, r.params, orth_tol=r.orth_tol,)
    else:
        r.max_vectors_before_pod = max(r.max_vectors_before_pod, len(r.modes) + len(r.gathered_modes))
        r.modes, r.svals = local_pod(
            [[r.modes, r.svals], r.gathered_modes],
            r.total_num_snapshots,
            r.params,
            orth_tol=r.orth_tol,
            incremental_gramian=r.incremental_gramian,
            root_of_tree=(chunk_index == num_chunks - 1 and mpi.size_rank_0_group == 1),
        )
    r.max_local_modes = max(r.max_local_modes, len(r.modes))


def final_hapod_in_binary_tree_hapod(r, mpi):
    print("start final pod: ", mpi.rank_world)
    (r.modes, r.svals, r.total_num_snapshots, max_num_input_vecs, max_num_local_modes,) = binary_tree_hapod_over_ranks(
        mpi.comm_rank_0_group,
        r.modes,
        r.total_num_snapshots,
        r.params,
        svals=r.svals,
        last_hapod=True,
        incremental_gramian=r.incremental_gramian,
        orth_tol=r.orth_tol,
    )
    r.max_vectors_before_pod = max(r.max_vectors_before_pod, max_num_input_vecs)
    r.max_local_modes = max(r.max_local_modes, max_num_local_modes)


def binary_tree_hapod(
    cellmodel,
    mus,
    chunk_size,
    mpi,
    tolerances,
    indices,
    include_newton_stages=False,
    omega=0.95,
    return_snapshots=False,
    return_newton_residuals=False,
    incremental_gramian=True,
    orth_tol=1e-10,
    logfile=None,
):

    assert isinstance(cellmodel, CellModel)
    assert isinstance(mpi, MPIWrapper)
    assert len(tolerances) == 6
    assert len(pod_indices) <= 3
    assert len(deim_indices) <= 3

    # setup timings
    timings = {}
    timings['data'] = 0.
    for k in indices:
        timings[f'POD{k}'] = 0.

    # calculate rooted tree depth
    mpi = MPIWrapper()
    num_chunks, _ = solver_statistics(cellmodel, chunk_size, with_half_steps=False)
    node_binary_tree_depth = binary_tree_depth(mpi.comm_rank_0_group)
    node_binary_tree_depth = mpi.comm_proc.bcast(node_binary_tree_depth, root=0)
    tree_depth = num_chunks + node_binary_tree_depth

    # create classes that store HAPOD results and parameters for easier handling
    results = [BinaryTreeHapodResults(tree_depth, tol, omega) for tol in tolerances]
    # add some more properties to the results classes
    for r in results:
        r.orth_tol = orth_tol
        r.incremental_gramian = incremental_gramian

    # store initial values
    space = cellmodel.solution_space
    current_values = [cellmodel.initial_values.copy() for _ in range(len(mus))]
    if return_snapshots:
        U = [space.empty() for _ in range(len(mus))]
        for p in range(len(mus)):
            U[p].append(current_values[p])
    if return_newton_residuals:
        U_res = [tuple(space.subspaces[i].empty() for i in range(3)) for _ in range(len(mus))]
    # walk over time chunks
    # currently, all parameters have to use the same timestep length in all time steps
    old_time = 0.0
    for chunk_index in range(num_chunks):
        new_vecs = [U[0]._blocks[i % 3].empty() for i in range(6)]
        # walk over parameters
        for p in range(len(mus)):
            time = old_time
            mu = mus[p]
            # If this is the first time step, add initial values ...
            if chunk_index == 0:
                for k in pod_indices:
                    new_vecs[k].append(current_values[p]._blocks[k])
            # ... and do one timestep less to ensure that chunk has size chunk_size
            for time_index in range(chunk_size - 1 if chunk_index == 0 else chunk_size):
                t1 = timer()
                retval = cellmodel.next_time_step(
                    current_values[p], time, mu=mu, return_stages=include_newton_stages, return_residuals=True,
                )
                timings['data']  += timer() - t1
                current_values[p], time, *retval = retval
                if include_newton_stages:
                    timestep_stages, *retval = retval
                timestep_residuals, *retval = retval
                # check if we are finished (if time == t_end cellmodel.next_time_step returns None)
                if current_values[p] is None:
                    assert chunk_index == num_chunks - 1
                    assert time_index != 0
                    break
                # store data (if requested)
                if return_snapshots:
                    U[p].append(current_values[p])
                if return_newton_residuals:
                    for q in range(3):
                        U_res[p][q].append(timestep_residuals[q])
                # store POD input
                for k in indices:
                    if k < 3:
                        # this is a POD index
                        new_vecs[k].append(current_values[p]._blocks[k])
                        if include_newton_stages:
                            new_vecs[k].append(timestep_stages[k])
                    else:
                        # this is a DEIM index
                        new_vecs[k].append(timestep_residuals[k - 3])
        old_time = time

        # calculate POD of timestep vectors on each core
        for k in indices:
            t1 = timer()
            pods_on_processor_cores_in_binary_tree_hapod(results[k], new_vecs[k])
            if mpi.rank_proc == 0:
                # perform another pod on rank 0 with gathered modes and modes from the last chunk
                pod_on_node_in_binary_tree_hapod(results[k], chunk_index, num_chunks, mpi)
            timings[f'POD{k}'] += timer() - t1

    # Finally, perform a HAPOD over a binary tree of nodes
    for k in indices:
        r = results[k]
        if mpi.rank_proc == 0:
            t1 = timer()
            final_hapod_in_binary_tree_hapod(r, mpi)
            timings[f'POD{k}'] += timer() - t1

        # calculate max number of local modes
        r.max_vectors_before_pod = mpi.comm_world.gather(r.max_vectors_before_pod, root=0)
        r.max_local_modes = mpi.comm_world.gather(r.max_local_modes, root=0)
        if mpi.rank_world == 0:
            r.max_vectors_before_pod = max(r.max_vectors_before_pod)
            r.max_local_modes = max(r.max_local_modes)

    # write statistics to file
    if logfile is not None and mpi.rank_world == 0:
        with open(logfile, "a") as ff:
            ff.write(f"Data computation took {timings['data']} s.\n")
            for k in indices:
                r = results[k]
                ff.write(f"Hapod for index {k}\n")
                ff.write(
                    f"The HAPOD resulted in {len(r.modes)} final modes taken from a total of {r.total_num_snapshots} snapshots!\n"
                )
                ff.write(f"The maximal number of local modes was {r.max_local_modes}\n")
                ff.write(f"The maximal number of input vectors to a local POD was: {r.max_vectors_before_pod}\n")
                ff.write("PODs took {} s.\n".format(timings[f"POD{k}"]))
            ff.write(
                f"The maximum amount of memory used on rank 0 was: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1000.0 ** 2} GB\n"
            )
    mpi.comm_world.Barrier()
    if not return_snapshots:
        U = None
    if not return_newton_residuals:
        U_res = None
    return U, U_res, results


if __name__ == "__main__":
    mpi = MPIWrapper()
    ##### read command line arguments, additional settings #####
    argc = len(sys.argv)
    testcase = "single_cell" if argc < 2 else sys.argv[1]
    t_end = 1e-1 if argc < 3 else float(sys.argv[2])
    dt = 1e-2 if argc < 4 else float(sys.argv[3])
    grid_size_x = 60 if argc < 5 else int(sys.argv[4])
    grid_size_y = 60 if argc < 6 else int(sys.argv[5])
    visualize = True if argc < 7 else (False if sys.argv[6] == "False" else True)
    subsampling = True if argc < 8 else (False if sys.argv[7] == "False" else True)
    deim_pfield = True if argc < 9 else (False if sys.argv[8] == "False" else True)
    deim_ofield = True if argc < 10 else (False if sys.argv[9] == "False" else True)
    deim_stokes = True if argc < 11 else (False if sys.argv[10] == "False" else True)
    include_newton_stages = False if argc < 12 else (False if sys.argv[11] == "False" else True)
    pfield_atol = 1e-3 if argc < 13 else float(sys.argv[12])
    ofield_atol = 1e-3 if argc < 14 else float(sys.argv[13])
    stokes_atol = 1e-3 if argc < 15 else float(sys.argv[14])
    pfield_deim_atol = 1e-10 if argc < 16 else float(sys.argv[15])
    ofield_deim_atol = 1e-10 if argc < 17 else float(sys.argv[16])
    stokes_deim_atol = 1e-10 if argc < 18 else float(sys.argv[17])
    pol_order = 1
    chunk_size = 10
    visualize_step = 50
    pod_pfield = True
    pod_ofield = True
    pod_stokes = True
    least_squares_pfield = True
    least_squares_ofield = True
    least_squares_stokes = True
    excluded_param = "Be"
    # default values for parameters
    Ca0 = 0.1
    Be0 = 0.3
    Pa0 = 1.0
    mus = []

    ####### Collect some settings in lists for simpler handling #####
    hapod_tols = [
        pfield_atol,
        ofield_atol,
        stokes_atol,
        pfield_deim_atol,
        ofield_deim_atol,
        stokes_deim_atol,
    ]
    # store fields that should be reduced (0=pfield, 1=ofield, 2=stokes)
    pod_indices = []
    deim_indices = []
    if pod_pfield:
        pod_indices.append(0)
    if deim_pfield:
        deim_indices.append(0)
    if pod_ofield:
        pod_indices.append(1)
    if deim_ofield:
        deim_indices.append(1)
    if pod_stokes:
        pod_indices.append(2)
    if deim_stokes:
        deim_indices.append(2)
    # 0=pfield, 1=ofield, 2=stokes, 3=pfield_deim, 4=ofield_deim, 5=stokes_deim
    indices = pod_indices.copy()
    for index in deim_indices:
        indices.append(index + 3)

    ####### Create training parameters ######
    train_params_per_rank = 1 if mpi.size_world > 1 else 1
    test_params_per_rank = 1 if mpi.size_world > 1 else 1
    num_train_params = train_params_per_rank * mpi.size_world
    num_test_params = test_params_per_rank * mpi.size_world
    # we have two parameters and want to sample both of these parameters with the same number of values
    values_per_parameter_train = int(math.sqrt(num_train_params))
    values_per_parameter_test = int(math.sqrt(num_test_params))

    # Factor of largest to smallest training parameter
    rf = 10
    rf = np.sqrt(rf)
    lower_bound_Ca = Ca0 / rf
    upper_bound_Ca = Ca0 * rf
    lower_bound_Be = Be0 / rf
    upper_bound_Be = Be0 * rf
    lower_bound_Pa = Pa0 / rf
    upper_bound_Pa = Pa0 * rf
    # Compute factors such that mu_i/mu_{i+1} = const and mu_0 = default_value/sqrt(rf), mu_last = default_value*sqrt(rf)
    # factors = np.array([1.])
    factors = np.array(
        [(rf ** 2) ** (i / (values_per_parameter_train - 1)) / rf for i in range(values_per_parameter_train)]
        if values_per_parameter_train > 1
        else [1.0]
    )
    # Actually create training parameters.
    # Currently, only two parameters vary, the other one is set to the default value
    random.seed(123)
    if excluded_param == "Ca":
        for Be in factors * Be0:
            for Pa in factors * Pa0:
                mus.append({"Pa": Pa, "Be": Be, "Ca": Ca0})
    elif excluded_param == "Be":
        for Ca in factors * Ca0:
            for Pa in factors * Pa0:
                mus.append({"Pa": Pa, "Be": Be0, "Ca": Ca})
    elif excluded_param == "Pa":
        for Ca in factors * Ca0:
            for Be in factors * Be0:
                mus.append({"Pa": Pa0, "Be": Be, "Ca": Ca})
    else:
        raise NotImplementedError(f"Wrong value of excluded_param: {excluded_param}")
    while len(mus) < num_train_params:
        mus.append(
            {
                "Pa": Pa0 if excluded_param == "Pa" else random.uniform(lower_bound_Pa, upper_bound_Pa),
                "Be": Be0 if excluded_param == "Be" else random.uniform(lower_bound_Be, upper_bound_Be),
                "Ca": Ca0 if excluded_param == "Ca" else random.uniform(lower_bound_Ca, upper_bound_Ca),
            }
        )
    ####### Create test parameters ########
    new_mus = []
    for i in range(num_test_params):
        new_mus.append(
            {
                "Pa": Pa0 if excluded_param == "Pa" else random.uniform(lower_bound_Pa, upper_bound_Pa),
                "Be": Be0 if excluded_param == "Be" else random.uniform(lower_bound_Be, upper_bound_Be),
                "Ca": Ca0 if excluded_param == "Ca" else random.uniform(lower_bound_Ca, upper_bound_Ca),
            }
        )

    ###### Start writing output file #########

    filename = "rom_grid{}x{}_tend{}_dt{}_{}_without{}_pfield{}_ofield{}_stokes{}.txt".format(
        grid_size_x,
        grid_size_y,
        t_end,
        dt,
        "snapsandstages" if include_newton_stages else "snaps",
        excluded_param,
        (f"pod{pfield_atol:.0e}" if pod_pfield else "pod0")
        + (f"deim{pfield_deim_atol:.0e}" if deim_pfield else "deim0"),
        (f"pod{ofield_atol:.0e}" if pod_ofield else "pod0")
        + (f"deim{ofield_deim_atol:.0e}" if deim_ofield else "deim0"),
        (f"pod{stokes_atol:.0e}" if pod_stokes else "pod0")
        + (f"deim{stokes_deim_atol:.0e}" if deim_stokes else "deim0"),
    )

    if mpi.rank_world == 0:
        with open(filename, "w") as ff:
            ff.write(
                f"{filename}\nTrained with {len(mus)} Parameters: {mus}\n"
                f"Tested with {len(new_mus)} new Parameters: {new_mus}\n"
            )

    ####### Scatter parameters to MPI ranks #######
    # Transform mus and new_mus from plain list to list of lists where the i-th inner list contains all parameters for rank i
    mus = np.reshape(np.array(mus), (mpi.size_world, train_params_per_rank)).tolist()
    new_mus = np.reshape(np.array(new_mus), (mpi.size_world, test_params_per_rank)).tolist()
    mus = mpi.comm_world.scatter(mus, root=0)
    new_mus = mpi.comm_world.scatter(new_mus, root=0)
    solver = CellModelSolver(testcase, t_end, dt, grid_size_x, grid_size_y, pol_order, mus[0])
    num_cells = solver.num_cells
    m = DuneCellModel(solver)
    Us, _, results = binary_tree_hapod(
        m,
        mus,
        chunk_size,
        mpi,
        hapod_tols,
        indices,
        include_newton_stages,
        omega=0.95,
        return_snapshots=True,
        return_newton_residuals=False,
        logfile=filename,
    )
    U = Us[0]
    # U_res = Us_res[0]
    for p in range(1, len(mus)):
        U.append(Us[p])
    #     for q in range(3):
    #         U_res[q].append(Us_res[p][q])
    del Us
    for k in indices:
        r = results[k]
        r.modes, r.win = mpi.shared_memory_bcast_modes(r.modes, returnlistvectorarray=True)

    pfield_basis = results[0].modes if pod_pfield else None
    ofield_basis = results[1].modes if pod_ofield else None
    stokes_basis = results[2].modes if pod_stokes else None
    pfield_deim_basis = results[3].modes if deim_pfield else None
    ofield_deim_basis = results[4].modes if deim_ofield else None
    stokes_deim_basis = results[5].modes if deim_stokes else None

    reduced_prefix = "without{}_{}_pfield_{}_ofield_{}_stokes_{}".format(
        excluded_param,
        "snapsandstages" if include_newton_stages else "snaps",
        (f"pod{pfield_atol:.0e}" if pod_pfield else "pod0")
        + (f"deim{pfield_deim_atol:.0e}" if deim_pfield else "deim0"),
        (f"pod{ofield_atol:.0e}" if pod_ofield else "pod0")
        + (f"deim{ofield_deim_atol:.0e}" if deim_ofield else "deim0"),
        (f"pod{stokes_atol:.0e}" if pod_stokes else "pod0")
        + (f"deim{stokes_deim_atol:.0e}" if deim_stokes else "deim0"),
    )
    reductor = CellModelReductor(
        m,
        pfield_basis,
        ofield_basis,
        stokes_basis,
        least_squares_pfield=least_squares_pfield,
        least_squares_ofield=least_squares_ofield,
        least_squares_stokes=least_squares_stokes,
        pfield_deim_basis=pfield_deim_basis,
        ofield_deim_basis=ofield_deim_basis,
        stokes_deim_basis=stokes_deim_basis,
    )
    rom = reductor.reduce()

    ################## solve reduced model for trained parameters ####################
    # print("Hello", flush=True)
    # time.sleep(10)
    u = rom.solve(mus[0], return_stages=False)
    for p in range(1, len(mus)):
        u.append(rom.solve(mus[p], return_stages=False))
    U_rom = reductor.reconstruct(u)

    ################## calculate errors for trained parameters #######################
    pfield_abs_errors = (U._blocks[0] - U_rom._blocks[0]).norm()
    ofield_abs_errors = (U._blocks[1] - U_rom._blocks[1]).norm()
    stokes_abs_errors = (U._blocks[2] - U_rom._blocks[2]).norm()
    pfield_rel_errors = (U._blocks[0] - U_rom._blocks[0]).norm() / U._blocks[0].norm()
    ofield_rel_errors = (U._blocks[1] - U_rom._blocks[1]).norm() / U._blocks[1].norm()
    stokes_rel_errors = (U._blocks[2] - U_rom._blocks[2]).norm() / U._blocks[2].norm()
    pfield_norms = U._blocks[0].norm()
    ofield_norms = U._blocks[1].norm()
    stokes_norms = U._blocks[2].norm()
    pfield_abs_errors = mpi.comm_world.gather(pfield_abs_errors, root=0)
    ofield_abs_errors = mpi.comm_world.gather(ofield_abs_errors, root=0)
    stokes_abs_errors = mpi.comm_world.gather(stokes_abs_errors, root=0)
    pfield_rel_errors = mpi.comm_world.gather(pfield_rel_errors, root=0)
    ofield_rel_errors = mpi.comm_world.gather(ofield_rel_errors, root=0)
    stokes_rel_errors = mpi.comm_world.gather(stokes_rel_errors, root=0)
    pfield_norms = mpi.comm_world.gather(pfield_norms, root=0)
    ofield_norms = mpi.comm_world.gather(ofield_norms, root=0)
    stokes_norms = mpi.comm_world.gather(stokes_norms, root=0)
    del U, U_rom

    ################## test new parameters #######################
    # solve full-order model for new param
    start = timer()
    U_new_mu = m.solve(mu=new_mus[0], return_stages=False)
    for p in range(1, len(new_mus)):
        U_new_mu.append(m.solve(mu=new_mus[p], return_stages=False))
    mean_fom_time = (timer() - start) / len(new_mus)
    # Be, Ca, Pa = (float(new_mu['Be']), float(new_mu['Ca']), float(new_mu['Pa']))
    # m.visualize(U_new_mu, prefix=f"fullorder_Be{Be}_Ca{Ca}_Pa{Pa}", subsampling=subsampling, every_nth=visualize_step)

    # solve reduced model for new params
    start = timer()
    # cProfile.run("u_new_mu = rom.solve(new_mus[0], return_stages=False)", f"rom{mpi.rank_world}.cprof")
    u_new_mu = rom.solve(new_mus[0], return_stages=False)
    for p in range(1, len(new_mus)):
        u_new_mu.append(rom.solve(new_mus[p], return_stages=False))
    mean_rom_time = (timer() - start) / len(new_mus)
    U_rom_new_mu = reductor.reconstruct(u_new_mu)
    # Be, Ca, Pa = (float(new_mu['Be']), float(new_mu['Ca']), float(new_mu['Pa']))
    # m.visualize(
    #     U_rom_new_mu,
    #     prefix=f"{reduced_prefix}_Be{Be}_Ca{Ca}_Pa{Pa}",
    #     subsampling=subsampling,
    #     every_nth=visualize_step)

    # calculate_errors for new mus
    pfield_abs_errors_new_mu = (U_new_mu._blocks[0] - U_rom_new_mu._blocks[0]).norm()
    ofield_abs_errors_new_mu = (U_new_mu._blocks[1] - U_rom_new_mu._blocks[1]).norm()
    stokes_abs_errors_new_mu = (U_new_mu._blocks[2] - U_rom_new_mu._blocks[2]).norm()
    pfield_rel_errors_new_mu = (U_new_mu._blocks[0] - U_rom_new_mu._blocks[0]).norm() / U_new_mu._blocks[0].norm()
    ofield_rel_errors_new_mu = (U_new_mu._blocks[1] - U_rom_new_mu._blocks[1]).norm() / U_new_mu._blocks[1].norm()
    stokes_rel_errors_new_mu = (U_new_mu._blocks[2] - U_rom_new_mu._blocks[2]).norm() / U_new_mu._blocks[2].norm()
    mean_fom_time = mpi.comm_world.gather(mean_fom_time, root=0)
    mean_rom_time = mpi.comm_world.gather(mean_rom_time, root=0)
    pfield_abs_errors_new_mus = mpi.comm_world.gather(pfield_abs_errors_new_mu, root=0)
    ofield_abs_errors_new_mus = mpi.comm_world.gather(ofield_abs_errors_new_mu, root=0)
    stokes_abs_errors_new_mus = mpi.comm_world.gather(stokes_abs_errors_new_mu, root=0)
    pfield_rel_errors_new_mus = mpi.comm_world.gather(pfield_rel_errors_new_mu, root=0)
    ofield_rel_errors_new_mus = mpi.comm_world.gather(ofield_rel_errors_new_mu, root=0)
    stokes_rel_errors_new_mus = mpi.comm_world.gather(stokes_rel_errors_new_mu, root=0)

    if mpi.rank_world == 0:
        pfield_abs_errors = np.concatenate(pfield_abs_errors)
        ofield_abs_errors = np.concatenate(ofield_abs_errors)
        stokes_abs_errors = np.concatenate(stokes_abs_errors)
        pfield_rel_errors = np.concatenate(pfield_rel_errors)
        ofield_rel_errors = np.concatenate(ofield_rel_errors)
        stokes_rel_errors = np.concatenate(stokes_rel_errors)
        pfield_norms = np.concatenate(pfield_norms)
        ofield_norms = np.concatenate(ofield_norms)
        stokes_norms = np.concatenate(stokes_norms)
        mean_fom_time = np.mean(mean_fom_time)
        mean_rom_time = np.mean(mean_rom_time)
        pfield_abs_errors_new_mus = np.concatenate(pfield_abs_errors_new_mus)
        ofield_abs_errors_new_mus = np.concatenate(ofield_abs_errors_new_mus)
        stokes_abs_errors_new_mus = np.concatenate(stokes_abs_errors_new_mus)
        pfield_rel_errors_new_mus = np.concatenate(pfield_rel_errors_new_mus)
        ofield_rel_errors_new_mus = np.concatenate(ofield_rel_errors_new_mus)
        stokes_rel_errors_new_mus = np.concatenate(stokes_rel_errors_new_mus)
        np.set_printoptions(formatter={"float": "{:.2e}".format})
        with open(filename, "a") as ff:
            ff.write(
                "tol_pf tol_of tol_st tol_deim_pf tol_deim_of tol_deim_st n_pf n_of n_st n_deim_pf n_deim_of n_deim_st mean_err_pf mean_err_of mean_err_st mean_rel_err_pf mean_rel_err_of mean_rel_err_st mean_err_pf_new mean_err_of_new mean_err_st_new mean_rel_err_pf_new mean_rel_err_of_new mean_rel_err_st_new mean_norm_pf mean_norm_of mean_norm_st\n"
            )
            ff.write(
                ("{} " * 12 + "{:.2e} " * 15 + "\n").format(
                    pfield_atol,
                    ofield_atol,
                    stokes_atol,
                    pfield_deim_atol,
                    ofield_deim_atol,
                    stokes_deim_atol,
                    len(pfield_basis),
                    len(ofield_basis),
                    len(stokes_basis),
                    len(pfield_deim_basis) if pfield_deim_basis else 0,
                    len(ofield_deim_basis) if ofield_deim_basis else 0,
                    len(stokes_deim_basis) if stokes_deim_basis else 0,
                    mean([err for err in pfield_abs_errors if not np.isnan(err)]),
                    mean([err for err in ofield_abs_errors if not np.isnan(err)]),
                    mean([err for err in stokes_abs_errors if not np.isnan(err)]),
                    mean([err for err in pfield_rel_errors if not np.isnan(err)]),
                    mean([err for err in ofield_rel_errors if not np.isnan(err)]),
                    mean([err for err in stokes_rel_errors if not np.isnan(err)]),
                    mean([err for err in pfield_abs_errors_new_mus if not np.isnan(err)]),
                    mean([err for err in ofield_abs_errors_new_mus if not np.isnan(err)]),
                    mean([err for err in stokes_abs_errors_new_mus if not np.isnan(err)]),
                    mean([err for err in pfield_rel_errors_new_mus if not np.isnan(err)]),
                    mean([err for err in ofield_rel_errors_new_mus if not np.isnan(err)]),
                    mean([err for err in stokes_rel_errors_new_mus if not np.isnan(err)]),
                    mean([norm for norm in pfield_norms if not np.isnan(norm)]),
                    mean([norm for norm in ofield_norms if not np.isnan(norm)]),
                    mean([norm for norm in stokes_norms if not np.isnan(norm)]),
                )
            )
            ff.write(
                f"\nAbsolute errors for trained mus:\n {pfield_abs_errors}\n {ofield_abs_errors}\n {stokes_abs_errors}\n"
            )
            ff.write(
                f"\nAbsolute errors for new mus:\n {pfield_abs_errors_new_mus}\n {ofield_abs_errors_new_mus}\n {stokes_abs_errors_new_mus}\n"
            )
            ff.write(
                f"\nRelative errors for trained mus:\n {pfield_rel_errors}\n {ofield_rel_errors}\n {stokes_rel_errors}\n"
            )
            ff.write(
                f"\nRelative errors for new mus:\n {pfield_rel_errors_new_mus}\n {ofield_rel_errors_new_mus}\n {stokes_rel_errors_new_mus}\n"
            )
            ff.write(f"\nTimings\n {mean_fom_time} vs. {mean_rom_time}, speedup {mean_fom_time/mean_rom_time}\n")
        print(
            "****",
            len(pfield_basis),
            len(ofield_basis),
            len(stokes_basis),
            len(pfield_deim_basis) if pfield_deim_basis is not None else 0,
            len(ofield_deim_basis) if ofield_deim_basis is not None else 0,
            len(stokes_deim_basis) if stokes_deim_basis is not None else 0,
        )
        print("Trained mus")
        print(pfield_rel_errors)
        print(ofield_rel_errors)
        print(stokes_rel_errors)
        print("New mus")
        print(pfield_rel_errors_new_mus)
        print(ofield_rel_errors_new_mus)
        print(stokes_rel_errors_new_mus)
        print("Timings")
        print(f"{mean_fom_time:.2f} vs. {mean_rom_time:.2f}, speedup {mean_fom_time/mean_rom_time:.2f}")
