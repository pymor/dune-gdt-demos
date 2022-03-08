import math
from numbers import Number
import pickle
import random
import resource
from statistics import mean
from timeit import default_timer as timer
from typing import Any, Union
import weakref

# import matplotlib.pyplot as plt

import gdt.cellmodel
from hapod.hapod import binary_tree_depth
from hapod.hapod import HapodParameters, binary_tree_hapod_over_ranks, local_pod
from hapod.mpi import MPIWrapper, idle_wait
from hapod.xt import DuneXtLaListVectorSpace, DuneXtLaVector
import numpy as np
from pymor.algorithms.ei import deim
from pymor.algorithms.newton import newton, newton_two_ops
from pymor.algorithms.projection import project
from pymor.core.base import abstractmethod
from pymor.models.interface import Model
from pymor.operators.constructions import ProjectedOperator, VectorOperator
from pymor.operators.ei import EmpiricalInterpolatedOperator, ProjectedEmpiricalInterpolatedOperator
from pymor.operators.interface import Operator
from pymor.operators.numpy import NumpyMatrixOperator
from pymor.parameters.base import Parameters, ParametricObject
from pymor.reductors.basic import ProjectionBasedReductor
from pymor.vectorarrays.block import BlockVectorSpace
from pymor.vectorarrays.interface import VectorArray
from pymor.vectorarrays.list import ListVectorArray
from pymor.vectorarrays.numpy import NumpyVectorArray, NumpyVectorSpace

# Parameters are Be, Ca, Pa
CELLMODEL_PARAMETER_TYPE = Parameters({"Be": 1, "Ca": 1, "Pa": 1})

NEWTON_PARAMS = {
    "stagnation_threshold": 0.99,
    "stagnation_window": 2,
    "maxiter": 10000,
    "relax": 1.0,
    "rtol": 1e-14,
    "atol": 1e-11,
    "error_measure": "residual",
}


class CellModelSolver(ParametricObject):
    def __init__(self, testcase, t_end, dt, grid_size_x, grid_size_y, pol_order, mu):
        self.__auto_init(locals())
        self.impl = gdt.cellmodel.CellModelSolver(
            testcase,
            t_end,
            dt,
            grid_size_x,
            grid_size_y,
            pol_order,
            False,
            float(mu["Be"]),
            float(mu["Ca"]),
            float(mu["Pa"]),
        )
        self._last_mu = mu
        self._last_pfield_dofs = None
        self._last_ofield_dofs = None
        self._last_stokes_dofs = None
        self.pfield_solution_space = DuneXtLaListVectorSpace(self.impl.pfield_vec(0).size)
        self.pfield_numpy_space = NumpyVectorSpace(self.impl.pfield_vec(0).size)
        self.ofield_solution_space = DuneXtLaListVectorSpace(self.impl.ofield_vec(0).size)
        self.stokes_solution_space = DuneXtLaListVectorSpace(self.impl.stokes_vec().size)
        self.num_cells = self.impl.num_cells()

    def linear(self):
        return self.impl.linear()

    def reset(self):
        return self.impl.reset()

    def solve(self, write=False, write_step=0, filename="", subsampling=True):
        return self.dune_result_to_pymor(self.impl.solve(write, write_step, filename, subsampling))

    def next_n_timesteps(self, n):
        return self.dune_result_to_pymor(self.impl.next_n_timesteps(n))

    def dune_result_to_pymor(self, result):
        ret = []
        nc = self.num_cells
        for k in range(nc):
            ret.append(self.pfield_solution_space.make_array(result[k]))
        for k in range(nc):
            ret.append(self.ofield_solution_space.make_array(result[nc + k]))
        ret.append(self.stokes_solution_space.make_array(result[2 * nc]))
        return ret

    def visualize(self, prefix, num, t, subsampling=True):
        self.impl.visualize(prefix, num, t, subsampling)

    def visualize_pfield(self, filename, vec, subsampling=True):
        self.impl.visualize_pfield(filename, vec.impl, subsampling)

    def visualize_ofield(self, filename, vec, subsampling=True):
        self.impl.visualize_ofield(filename, vec.impl, subsampling)

    def visualize_stokes(self, filename, vec, subsampling=True):
        self.impl.visualize_stokes(filename, vec.impl, subsampling)

    def finished(self):
        return self.impl.finished()

    def apply_pfield_L2_product_operator(self, U, mu=None, numpy=False):
        pfield_space = self.pfield_solution_space
        if not numpy:
            U_out = [self.impl.apply_pfield_L2_product_operator(vec.impl) for vec in U._list]
            return pfield_space.make_array(U_out)
        else:
            U_list = pfield_space.make_array([pfield_space.vector_from_numpy(vec).impl for vec in U.to_numpy()])
            U_out = [
                DuneXtLaVector(self.impl.apply_pfield_L2_product_operator(vec.impl)).to_numpy(True)
                for vec in U_list._list
            ]
            return self.pfield_numpy_space.make_array(U_out)

    def apply_ofield_L2_product_operator(self, U, mu=None):
        U_out = [self.impl.apply_ofield_L2_product_operator(vec.impl) for vec in U._list]
        return self.ofield_solution_space.make_array(U_out)

    def apply_stokes_L2_product_operator(self, U, mu=None):
        U_out = [self.impl.apply_stokes_L2_product_operator(vec.impl) for vec in U._list]
        return self.stokes_solution_space.make_array(U_out)

    def apply_pfield_H1_product_operator(self, U, mu=None):
        U_out = [self.impl.apply_pfield_H1_product_operator(vec.impl) for vec in U._list]
        return self.pfield_solution_space.make_array(U_out)

    def apply_pfield_H1_product_operator(self, U, mu=None):
        U_out = [self.impl.apply_pfield_H1_product_operator(vec.impl) for vec in U._list]
        return self.pfield_solution_space.make_array(U_out)

    def pfield_vector(self, cell_index):
        return DuneXtLaVector(self.impl.pfield_vec(cell_index))

    def ofield_vector(self, cell_index):
        return DuneXtLaVector(self.impl.ofield_vec(cell_index))

    def stokes_vector(self):
        return DuneXtLaVector(self.impl.stokes_vec())

    def compute_pfield_deim_dofs(self, range_dofs, cell_index=0):
        self.impl.compute_restricted_pfield_dofs([int(i) for i in range_dofs], cell_index)

    def compute_ofield_deim_dofs(self, range_dofs, cell_index=0):
        self.impl.compute_restricted_ofield_dofs([int(i) for i in range_dofs], cell_index)

    def compute_stokes_deim_dofs(self, range_dofs):
        self.impl.compute_restricted_stokes_dofs([int(i) for i in range_dofs])

    def pfield_deim_source_dofs(self, cell_index):
        return self.impl.pfield_deim_source_dofs(cell_index)

    def ofield_deim_source_dofs(self, cell_index):
        return self.impl.ofield_deim_source_dofs(cell_index)

    def stokes_deim_source_dofs(self):
        return self.impl.stokes_deim_source_dofs()

    def set_pfield_vec(self, cell_index, vec):
        assert isinstance(vec, DuneXtLaVector)
        assert vec.dim == self.pfield_solution_space.dim
        return self.impl.set_pfield_vec(cell_index, vec.impl)

    def set_ofield_vec(self, cell_index, vec):
        assert isinstance(vec, DuneXtLaVector)
        assert vec.dim == self.ofield_solution_space.dim
        return self.impl.set_ofield_vec(cell_index, vec.impl)

    def set_stokes_vec(self, vec):
        assert isinstance(vec, DuneXtLaVector)
        assert vec.dim == self.stokes_solution_space.dim
        return self.impl.set_stokes_vec(vec.impl)

    def set_pfield_vec_dofs(self, cell_index, vec, dofs):
        assert len(vec) == len(dofs)
        assert all([dof < self.pfield_solution_space.dim for dof in dofs])
        return self.impl.set_pfield_vec_dofs(cell_index, vec, dofs)

    def set_ofield_vec_dofs(self, cell_index, vec, dofs):
        assert len(vec) == len(dofs)
        assert all([dof < self.ofield_solution_space.dim for dof in dofs])
        return self.impl.set_ofield_vec_dofs(cell_index, vec, dofs)

    def set_stokes_vec_dofs(self, vec, dofs):
        assert len(vec) == len(dofs)
        assert all([dof < self.stokes_solution_space.dim for dof in dofs])
        return self.impl.set_stokes_vec_dofs(vec, dofs)

    def prepare_pfield_operator(self, cell_index, restricted=False):
        return self.impl.prepare_pfield_operator(cell_index, restricted)

    def prepare_ofield_operator(self, cell_index, restricted=False):
        return self.impl.prepare_ofield_operator(cell_index, restricted)

    def prepare_stokes_operator(self, restricted=False):
        return self.impl.prepare_stokes_operator(restricted)

    def set_pfield_jacobian_state(self, vec, cell_index):
        assert isinstance(vec, DuneXtLaVector)
        assert vec.dim == self.pfield_solution_space.dim
        self.impl.set_pfield_jacobian_state(vec.impl, cell_index)

    def set_ofield_jacobian_state(self, vec, cell_index):
        assert isinstance(vec, DuneXtLaVector)
        assert vec.dim == self.ofield_solution_space.dim
        self.impl.set_ofield_jacobian_state(vec.impl, cell_index)

    def set_pfield_jacobian_state_dofs(self, vec, cell_index):
        self.impl.set_pfield_jacobian_state_dofs(vec, cell_index)

    def set_ofield_jacobian_state_dofs(self, vec, cell_index):
        self.impl.set_ofield_jacobian_state_dofs(vec, cell_index)

    def apply_inverse_pfield_operator(self, guess_vec, cell_index):
        assert isinstance(guess_vec, DuneXtLaVector)
        assert guess_vec.dim == self.pfield_solution_space.dim
        return self.pfield_solution_space.make_array(
            [self.impl.apply_inverse_pfield_operator(guess_vec.impl, cell_index)]
        )

    def apply_inverse_ofield_operator(self, guess_vec, cell_index):
        assert isinstance(guess_vec, DuneXtLaVector)
        assert guess_vec.dim == self.ofield_solution_space.dim
        return self.ofield_solution_space.make_array(
            [self.impl.apply_inverse_ofield_operator(guess_vec.impl, cell_index)]
        )

    def apply_inverse_stokes_operator(self):
        return self.stokes_solution_space.make_array([self.impl.apply_inverse_stokes_operator()])

    def apply_pfield_operator(self, U, cell_index, restricted=False):
        if restricted:
            U = self.numpy_vecarray_to_xt_listvecarray(U)
        else:
            assert U.dim == self.pfield_solution_space.dim
        # U_out = [self.impl.apply_pfield_residual_operator(vec.impl, cell_index, restricted) for vec in U._list]
        U_out = [
            self.impl.apply_pfield_residual_operator_without_diagonal_mass_matrices(vec.impl, cell_index, restricted)
            for vec in U._list
        ]
        return self.pfield_solution_space.make_array(U_out)

    def apply_pfield_lin_operator(self, U, restricted=False):
        assert U.dim == self.pfield_solution_space.dim
        U_out = [self.impl.apply_pfield_diagonal_mass_matrices(vec.impl, 0, restricted) for vec in U._list]
        return self.pfield_solution_space.make_array(U_out)
        # return self.pfield_solution_space.zeros(len(U._list))

    def apply_ofield_operator(self, U, cell_index, restricted=False):
        if restricted:
            U = self.numpy_vecarray_to_xt_listvecarray(U)
        else:
            assert U.dim == self.ofield_solution_space.dim
        U_out = [self.impl.apply_ofield_operator(vec.impl, cell_index, restricted) for vec in U._list]
        return self.ofield_solution_space.make_array(U_out)

    def apply_stokes_operator(self, U, restricted=False):
        if restricted:
            U = self.numpy_vecarray_to_xt_listvecarray(U)
        else:
            assert U.dim == self.stokes_solution_space.dim
        U_out = [self.impl.apply_stokes_operator(vec.impl, restricted) for vec in U._list]
        return self.stokes_solution_space.make_array(U_out)

    def apply_inverse_pfield_jacobian(self, V, cell_index):
        return self.pfield_solution_space.make_array(
            [self.impl.apply_inverse_pfield_jacobian(vec.impl, cell_index) for vec in V._list]
        )

    def apply_inverse_ofield_jacobian(self, V, cell_index):
        return self.ofield_solution_space.make_array(
            [self.impl.apply_inverse_ofield_jacobian(vec.impl, cell_index) for vec in V._list]
        )

    def apply_inverse_stokes_jacobian(self, V):
        return self.stokes_solution_space.make_array(
            [self.impl.apply_inverse_stokes_jacobian(vec.impl) for vec in V._list]
        )

    def numpy_vecarray_to_xt_listvecarray(self, U, copy=False):
        ret = DuneXtLaListVectorSpace.from_memory(U._data)
        return ret.copy() if copy else ret

    def apply_pfield_jacobian(self, U, cell_index, restricted=False):
        if restricted:
            U = self.numpy_vecarray_to_xt_listvecarray(U)
        U_out = [self.impl.apply_pfield_jacobian(vec.impl, cell_index, restricted) for vec in U._list]
        # U_out = [self.impl.apply_pfield_jacobian_without_diagonal_mass_matrices(vec.impl, cell_index, restricted) for vec in U._list]
        ret = self.pfield_solution_space.make_array(U_out)
        return ret

    def apply_ofield_jacobian(self, U, cell_index, restricted=False):
        if restricted:
            U = self.numpy_vecarray_to_xt_listvecarray(U)
        U_out = [self.impl.apply_ofield_jacobian(vec.impl, cell_index, restricted) for vec in U._list]
        return self.ofield_solution_space.make_array(U_out)

    def apply_stokes_jacobian(self, U, restricted=False):
        if restricted:
            U = self.numpy_vecarray_to_xt_listvecarray(U)
        U_out = [self.impl.apply_stokes_jacobian(vec.impl, restricted) for vec in U._list]
        return self.stokes_solution_space.make_array(U_out)

    def update_pfield_parameters(self, mu, cell_index=0, restricted=False):
        self.impl.update_pfield_parameters(float(mu["Be"]), float(mu["Ca"]), float(mu["Pa"]), cell_index, restricted)

    def update_ofield_parameters(self, mu, cell_index=0, restricted=False):
        self.impl.update_ofield_parameters(float(mu["Pa"]), cell_index, restricted)


class CellModelPfieldLinOperator(Operator):
    def __init__(self, solver):
        self.solver = solver
        self.source = self.range = self.solver.pfield_solution_space
        self.linear = True
        self._parameters = Parameters()  # the mass matrices are parameter-independent

    def apply(self, U, mu=None):
        return self.solver.apply_pfield_lin_operator(U)


class CellModelPfieldL2ProductOperator(Operator):
    def __init__(self, solver):
        self.solver = solver
        self.source = self.range = self.solver.pfield_solution_space
        self.linear = True

    def apply(self, U, mu=None, numpy=False):
        return self.solver.apply_pfield_L2_product_operator(U, numpy=numpy)


class CellModelOfieldL2ProductOperator(Operator):
    def __init__(self, solver):
        self.solver = solver
        self.source = self.range = self.solver.ofield_solution_space
        self.linear = True

    def apply(self, U, mu=None):
        return self.solver.apply_ofield_L2_product_operator(U)


class CellModelStokesL2ProductOperator(Operator):
    def __init__(self, solver):
        self.solver = solver
        self.source = self.range = self.solver.stokes_solution_space
        self.linear = True

    def apply(self, U, mu=None):
        return self.solver.apply_stokes_L2_product_operator(U)


class CellModelPfieldH1ProductOperator(Operator):
    def __init__(self, solver):
        self.solver = solver
        self.source = self.range = self.solver.pfield_solution_space
        self.linear = True

    def apply(self, U, mu=None):
        return self.solver.apply_pfield_H1_product_operator(U)


class CellModelOfieldH1ProductOperator(Operator):
    def __init__(self, solver):
        self.solver = solver
        self.source = self.range = self.solver.ofield_solution_space
        self.linear = True

    def apply(self, U, mu=None):
        return self.solver.apply_ofield_H1_product_operator(U)


class MutableStateComponentOperator(Operator):

    mutable_state_index = (1,)

    @abstractmethod
    def _change_state(self, component_value=None, mu=None):
        pass

    @abstractmethod
    def _fixed_component_apply(self, U):
        pass

    def _fixed_component_apply_inverse(self, V, least_squares=False):
        raise NotImplementedError

    def _fixed_component_jacobian(self, U):
        raise NotImplementedError

    def _set_state(self, dofs, mu):
        new_pfield_dofs = None if dofs[0] == self.solver._last_pfield_dofs else dofs[0]
        new_ofield_dofs = None if dofs[1] == self.solver._last_ofield_dofs else dofs[1]
        new_stokes_dofs = None if len(dofs) < 3 or dofs[2] == self.solver._last_stokes_dofs else dofs[2]
        new_mu = None if mu == self.solver._last_mu else mu
        if not new_pfield_dofs == new_ofield_dofs == new_stokes_dofs == new_mu == None:
            self._change_state(
                pfield_dofs=new_pfield_dofs, ofield_dofs=new_ofield_dofs, stokes_dofs=new_stokes_dofs, mu=new_mu
            )
        if new_pfield_dofs is not None:
            self.solver._last_pfield_dofs = new_pfield_dofs
        if new_ofield_dofs is not None:
            self.solver._last_ofield_dofs = new_ofield_dofs
        if new_stokes_dofs is not None:
            self.solver._last_stokes_dofs = new_stokes_dofs
        if new_mu is not None:
            self.solver._last_mu = mu.copy()

    @property
    def fixed_component_source(self):
        subspaces = tuple(s for i, s in enumerate(self.source.subspaces) if i not in self.mutable_state_index)
        return subspaces[0] if len(subspaces) == 1 else BlockVectorSpace(subspaces)

    def apply(self, U, mu):
        assert U in self.source
        op = self.fix_component(self.mutable_state_index, U._blocks[self.mutable_state_index])
        U = U._blocks[: self.mutable_state_index] + U._blocks[self.mutable_state_index + 1 :]
        if len(U) > 1:
            U = op.source.make_array(U)
        return op.apply(U, mu=mu)

    def fix_components(self, idx, U):
        if isinstance(idx, Number):
            idx = (idx,)
        if isinstance(U, VectorArray):
            U = (U,)
        assert len(idx) == len(U)
        assert all(len(u) == 1 for u in U)
        if idx != self.mutable_state_index:
            raise NotImplementedError
        assert all(u in self.source.subspaces[i] for u, i in zip(U, idx))
        return MutableStateFixedComponentOperator(self, U)


class MutableStateFixedComponentOperator(Operator):
    def __init__(self, operator, component_value):
        component_value = tuple(U.copy() for U in component_value)
        self.__auto_init(locals())
        self.source = operator.fixed_component_source
        self.range = operator.range
        self.linear = operator.linear

    def apply(self, U, mu=None):
        assert U in self.source
        self.operator._set_state(self.component_value, mu)
        return self.operator._fixed_component_apply(U)

    def apply_inverse(self, V, mu=None, least_squares=False):
        assert V in self.range
        self.operator._set_state(self.component_value, mu)
        try:
            return self.operator._fixed_component_apply_inverse(V, least_squares=least_squares)
        except NotImplementedError:
            return super().apply_inverse(V, mu=mu, least_squares=least_squares)

    def jacobian(self, U, mu=None):
        assert U in self.source
        assert len(U) == 1
        self.operator._set_state(self.component_value, mu)
        return self.operator._fixed_component_jacobian(U, mu=mu)


class MutableStateComponentJacobianOperator(MutableStateComponentOperator):

    _last_jacobian_value = None

    @abstractmethod
    def _change_jacobian_state(self, jacobian_value):
        pass

    def _need_to_invalidate_jacobian_state(self, pfield_changed, ofield_changed, stokes_changed, mu_changed):
        return pfield_changed or ofield_changed or stokes_changed or mu_changed

    @abstractmethod
    def _fixed_component_jacobian_apply(self, U):
        pass

    def _fixed_component_jacobian_apply_inverse(self, V, least_squares=False):
        raise NotImplementedError

    def _set_state(self, dofs, mu):
        mu_changed = mu != self.solver._last_mu
        pfield_changed = dofs[0] != self.solver._last_pfield_dofs
        ofield_changed = dofs[1] != self.solver._last_ofield_dofs
        stokes_changed = False if len(dofs) < 3 else dofs[2] != self.solver._last_stokes_dofs
        if self._need_to_invalidate_jacobian_state(pfield_changed, ofield_changed, stokes_changed, mu_changed):
            self._last_jacobian_value = None
        super()._set_state(dofs, mu)

    def _set_state_jacobian(self, dofs, mu, jacobian_value):
        self._set_state(dofs, mu)
        if jacobian_value != self._last_jacobian_value:
            self._change_jacobian_state(jacobian_value)
        self._last_jacobian_value = jacobian_value

    def _fixed_component_jacobian(self, U, mu=None):
        assert len(U) == 1
        assert mu is None or mu == self.solver._last_mu
        return MutableStateFixedComponentJacobianOperator(
            self,
            (self.solver._last_pfield_dofs, self.solver._last_ofield_dofs, self.solver._last_stokes_dofs),
            self.solver._last_mu,
            U,
        )


class MutableStateFixedComponentJacobianOperator(Operator):
    def __init__(self, operator, component_value, mu, jacobian_value):
        mu = mu.copy() if mu is not None else None
        jacobian_value = jacobian_value.copy()
        self.__auto_init(locals())
        self.source = operator.fixed_component_source
        self.range = operator.range
        self.linear = True
        self._parameters = Parameters()  # mu is already fixed, so no parameters

    def apply(self, U, mu=None):
        assert U in self.source
        self.operator._set_state_jacobian(self.component_value, self.mu, self.jacobian_value)
        return self.operator._fixed_component_jacobian_apply(U)

    def apply_inverse(self, V, mu=None, least_squares=False):
        assert V in self.range
        # assert mu == None or mu == self.mu # mu has to be set in the constructor
        self.operator._set_state_jacobian(self.component_value, self.mu, self.jacobian_value)
        return self.operator._fixed_component_jacobian_apply_inverse(V, least_squares=least_squares)

    def restricted(self, dofs):
        restricted_operator, source_dofs = self.operator.restricted(dofs)
        restricted_source_dofs = restricted_operator._fixed_component_source_dofs
        restricted_component_value = [
            NumpyVectorArray(
                value.dofs(source_dofs[restricted_operator.mutable_state_index[i]]),
                NumpyVectorSpace(len(self.component_value)),
            )
            for i, value in enumerate(self.component_value)
        ]
        restricted_jacobian_value = NumpyVectorSpace.make_array(self.jacobian_value.dofs(restricted_source_dofs))
        ret_op = self.with_(
            operator=restricted_operator,
            component_value=restricted_component_value,
            jacobian_value=restricted_jacobian_value,
        )
        return ret_op, restricted_source_dofs


class CellModelRestrictedPfieldOperator(MutableStateComponentJacobianOperator):

    mutable_state_index = (1, 2, 3)

    def __init__(self, solver, cell_index, range_dofs):
        self.__auto_init(locals())
        self.linear = False
        self.solver.compute_pfield_deim_dofs(self.range_dofs, self.cell_index)
        self.source_dofs = self.solver.pfield_deim_source_dofs(self.cell_index)
        self.source_dofs = [self.source_dofs[0], *self.source_dofs]
        self._fixed_component_source_dofs = self.source_dofs[0]
        self.source = BlockVectorSpace([NumpyVectorSpace(len(dofs)) for dofs in self.source_dofs])
        self.range = NumpyVectorSpace(len(range_dofs))
        self.parameters_own = CELLMODEL_PARAMETER_TYPE

    def _change_state(self, pfield_dofs=None, ofield_dofs=None, stokes_dofs=None, mu=None):
        if mu is not None:
            self.solver.update_pfield_parameters(mu, self.cell_index, restricted=True)
        if pfield_dofs is not None:
            self.solver.set_pfield_vec_dofs(0, pfield_dofs._data[0], self.source_dofs[1])
        if ofield_dofs is not None:
            self.solver.set_ofield_vec_dofs(0, ofield_dofs._data[0], self.source_dofs[2])
        if stokes_dofs is not None:
            self.solver.set_stokes_vec_dofs(stokes_dofs._data[0], self.source_dofs[3])
        if not pfield_dofs == ofield_dofs == stokes_dofs == mu == None:
            self.solver.prepare_pfield_operator(self.cell_index, restricted=True)

    def _change_jacobian_state(self, jacobian_value):
        assert jacobian_value.dim == len(self._fixed_component_source_dofs)
        self.solver.set_pfield_jacobian_state_dofs(jacobian_value.to_numpy().ravel(), self.cell_index)

    def _need_to_invalidate_jacobian_state(self, pfield_changed, ofield_changed, stokes_changed, mu_changed):
        return pfield_changed or mu_changed

    def _fixed_component_apply(self, U):
        return self.solver.apply_pfield_operator(U, self.cell_index, restricted=True)

    def _fixed_component_apply_inverse(self, V, least_squares=False):
        raise NotImplementedError

    def _fixed_component_jacobian_apply(self, U):
        return self.solver.apply_pfield_jacobian(U, self.cell_index, restricted=True)

    def _fixed_component_jacobian_apply_inverse(self, V, least_squares=False):
        raise NotImplementedError


class CellModelPfieldOperator(MutableStateComponentJacobianOperator):

    mutable_state_index = (1, 2, 3)

    def __init__(self, solver, cell_index):
        self.__auto_init(locals())
        self.linear = False
        self.source = BlockVectorSpace(
            [
                self.solver.pfield_solution_space,
                self.solver.pfield_solution_space,
                self.solver.ofield_solution_space,
                self.solver.stokes_solution_space,
            ]
        )
        self.range = self.solver.pfield_solution_space
        self.parameters_own = CELLMODEL_PARAMETER_TYPE

    def _change_state(self, pfield_dofs=None, ofield_dofs=None, stokes_dofs=None, mu=None):
        if mu is not None:
            self.solver.update_pfield_parameters(mu, self.cell_index, restricted=False)
        if pfield_dofs is not None:
            self.solver.set_pfield_vec(0, pfield_dofs._list[0])
        if ofield_dofs is not None:
            self.solver.set_ofield_vec(0, ofield_dofs._list[0])
        if stokes_dofs is not None:
            self.solver.set_stokes_vec(stokes_dofs._list[0])
        if not pfield_dofs == ofield_dofs == stokes_dofs == mu == None:
            self.solver.prepare_pfield_operator(self.cell_index, restricted=False)

    def _change_jacobian_state(self, jacobian_value):
        self.solver.set_pfield_jacobian_state(jacobian_value._list[0], self.cell_index)

    def _need_to_invalidate_jacobian_state(self, pfield_changed, ofield_changed, stokes_changed, mu_changed):
        return pfield_changed or mu_changed

    def _fixed_component_apply(self, U):
        return self.solver.apply_pfield_operator(U, self.cell_index)

    def _fixed_component_apply_inverse(self, V, least_squares=False):
        raise NotImplementedError
        assert sum(V.norm()) == 0.0, "Not implemented for non-zero rhs!"
        assert not least_squares, "Least squares not implemented!"
        return self.solver.apply_inverse_pfield_operator(self.solver.pfield_vector(self.cell_index), self.cell_index)

    def _fixed_component_jacobian_apply(self, U):
        return self.solver.apply_pfield_jacobian(U, self.cell_index)

    def _fixed_component_jacobian_apply_inverse(self, V, least_squares=False):
        raise NotImplementedError
        assert not least_squares, "Least squares not implemented!"
        return self.solver.apply_inverse_pfield_jacobian(V, self.cell_index)

    def restricted(self, dofs):
        restricted_op = CellModelRestrictedPfieldOperator(self.solver, self.cell_index, dofs)
        return restricted_op, restricted_op.source_dofs


class CellModelRestrictedOfieldOperator(MutableStateComponentJacobianOperator):

    mutable_state_index = (1, 2, 3)

    def __init__(self, solver, cell_index, range_dofs):
        self.__auto_init(locals())
        self.linear = False
        self.solver.compute_ofield_deim_dofs(self.range_dofs, self.cell_index)
        self.source_dofs = self.solver.ofield_deim_source_dofs(self.cell_index)
        self.source_dofs = [self.source_dofs[1], *self.source_dofs]
        self._fixed_component_source_dofs = self.source_dofs[0]
        self.source = BlockVectorSpace([NumpyVectorSpace(len(dofs)) for dofs in self.source_dofs])
        self.range = NumpyVectorSpace(len(range_dofs))
        self.parameters_own = {"Be": 1, "Ca": 1, "Pa": 1}

    def _change_state(self, pfield_dofs=None, ofield_dofs=None, stokes_dofs=None, mu=None):
        if mu is not None:
            self.solver.update_ofield_parameters(mu, self.cell_index, restricted=True)
        if pfield_dofs is not None:
            self.solver.set_pfield_vec_dofs(0, pfield_dofs._data[0], self.source_dofs[1])
        if ofield_dofs is not None:
            self.solver.set_ofield_vec_dofs(0, ofield_dofs._data[0], self.source_dofs[2])
        if stokes_dofs is not None:
            self.solver.set_stokes_vec_dofs(stokes_dofs._data[0], self.source_dofs[3])
        if not pfield_dofs == ofield_dofs == stokes_dofs == mu == None:
            self.solver.prepare_ofield_operator(self.cell_index, restricted=True)

    def _change_jacobian_state(self, jacobian_value):
        assert jacobian_value.dim == len(self._fixed_component_source_dofs)
        self.solver.set_ofield_jacobian_state_dofs(jacobian_value.to_numpy().ravel(), self.cell_index)

    def _need_to_invalidate_jacobian_state(self, pfield_changed, ofield_changed, stokes_changed, mu_changed):
        return ofield_changed or mu_changed

    def _fixed_component_apply(self, U):
        return self.solver.apply_ofield_operator(U, self.cell_index, restricted=True)

    def _fixed_component_apply_inverse(self, V, least_squares=False):
        raise NotImplementedError

    def _fixed_component_jacobian_apply(self, U):
        return self.solver.apply_ofield_jacobian(U, self.cell_index, restricted=True)

    def _fixed_component_jacobian_apply_inverse(self, V, least_squares=False):
        raise NotImplementedError


class CellModelOfieldOperator(MutableStateComponentJacobianOperator):

    mutable_state_index = (1, 2, 3)

    def __init__(self, solver, cell_index):
        self.__auto_init(locals())
        self.linear = False
        self.source = BlockVectorSpace(
            [
                self.solver.ofield_solution_space,
                self.solver.pfield_solution_space,
                self.solver.ofield_solution_space,
                self.solver.stokes_solution_space,
            ]
        )
        self.range = self.solver.ofield_solution_space
        self.parameters_own = Parameters({"Pa": 1})

    def _change_state(self, pfield_dofs=None, ofield_dofs=None, stokes_dofs=None, mu=None):
        if mu is not None:
            self.solver.update_ofield_parameters(mu, self.cell_index, restricted=False)
        if pfield_dofs is not None:
            self.solver.set_pfield_vec(0, pfield_dofs._list[0])
        if ofield_dofs is not None:
            self.solver.set_ofield_vec(0, ofield_dofs._list[0])
        if stokes_dofs is not None:
            self.solver.set_stokes_vec(stokes_dofs._list[0])
        if not pfield_dofs == ofield_dofs == stokes_dofs == mu == None:
            self.solver.prepare_ofield_operator(self.cell_index, restricted=False)

    def _change_jacobian_state(self, jacobian_value):
        self.solver.set_ofield_jacobian_state(jacobian_value._list[0], self.cell_index)

    def _need_to_invalidate_jacobian_state(self, pfield_changed, ofield_changed, stokes_changed, mu_changed):
        return ofield_changed or mu_changed

    def _fixed_component_apply(self, U):
        return self.solver.apply_ofield_operator(U, self.cell_index)

    def _fixed_component_apply_inverse(self, V, least_squares=False):
        raise NotImplementedError
        assert sum(V.norm()) == 0.0, "Not implemented for non-zero rhs!"
        assert not least_squares, "Least squares not implemented!"
        return self.solver.apply_inverse_ofield_operator(self.solver.ofield_vector(self.cell_index), self.cell_index)

    def _fixed_component_jacobian_apply(self, U):
        return self.solver.apply_ofield_jacobian(U, self.cell_index)

    def _fixed_component_jacobian_apply_inverse(self, V, least_squares=False):
        assert not least_squares, "Least squares not implemented!"
        return self.solver.apply_inverse_ofield_jacobian(V, self.cell_index)

    def restricted(self, dofs):
        restricted_op = CellModelRestrictedOfieldOperator(self.solver, self.cell_index, dofs)
        return restricted_op, restricted_op.source_dofs


class CellModelRestrictedStokesOperator(MutableStateComponentJacobianOperator):

    mutable_state_index = (1, 2)

    def __init__(self, solver, range_dofs):
        self.__auto_init(locals())
        self.linear = False
        self.solver.compute_stokes_deim_dofs(self.range_dofs)
        self.source_dofs = self.solver.stokes_deim_source_dofs()
        self.source_dofs = [self.source_dofs[2], self.source_dofs[0], self.source_dofs[1]]
        self._fixed_component_source_dofs = self.source_dofs[0]
        self.source = BlockVectorSpace([NumpyVectorSpace(len(dofs)) for dofs in self.source_dofs])
        self.range = NumpyVectorSpace(len(range_dofs))

    def _change_state(self, pfield_dofs=None, ofield_dofs=None, stokes_dofs=None, mu=None):
        if pfield_dofs is not None:
            self.solver.set_pfield_vec_dofs(0, pfield_dofs._data[0], self.source_dofs[1])
        if ofield_dofs is not None:
            self.solver.set_ofield_vec_dofs(0, ofield_dofs._data[0], self.source_dofs[2])
        if not pfield_dofs == ofield_dofs == None:
            self.solver.prepare_stokes_operator(restricted=True)

    def _change_jacobian_state(self, jacobian_value):
        pass

    def _need_to_invalidate_jacobian_state(self, pfield_changed, ofield_changed, stokes_changed, mu_changed):
        return False

    def _fixed_component_apply(self, U):
        return self.solver.apply_stokes_operator(U, restricted=True)

    def _fixed_component_apply_inverse(self, V, least_squares=False):
        raise NotImplementedError

    def _fixed_component_jacobian_apply(self, U):
        return self.solver.apply_stokes_jacobian(U, restricted=True)

    def _fixed_component_jacobian_apply_inverse(self, V, least_squares=False):
        raise NotImplementedError


class CellModelStokesOperator(MutableStateComponentJacobianOperator):

    mutable_state_index = (1, 2)

    def __init__(self, solver):
        self.__auto_init(locals())
        self.linear = False
        self.source = BlockVectorSpace(
            [self.solver.stokes_solution_space, self.solver.pfield_solution_space, self.solver.ofield_solution_space]
        )
        self.range = self.solver.stokes_solution_space

    def _change_state(self, pfield_dofs=None, ofield_dofs=None, stokes_dofs=None, mu=None):
        if pfield_dofs is not None:
            self.solver.set_pfield_vec(0, pfield_dofs._list[0])
        if ofield_dofs is not None:
            self.solver.set_ofield_vec(0, ofield_dofs._list[0])
        if not pfield_dofs == ofield_dofs == None:
            self.solver.prepare_stokes_operator(restricted=False)

    def _change_jacobian_state(self, jacobian_value):
        pass

    def _need_to_invalidate_jacobian_state(self, pfield_changed, ofield_changed, stokes_changed, mu_changed):
        return False

    def _fixed_component_apply(self, U):
        return self.solver.apply_stokes_operator(U, restricted=False)

    def _fixed_component_apply_inverse(self, V, least_squares=False):
        assert sum(V.norm()) == 0.0, "Not implemented for non-zero rhs!"
        assert not least_squares, "Least squares not implemented!"
        return self.solver.apply_inverse_stokes_operator()

    def _fixed_component_jacobian_apply(self, U):
        return self.solver.apply_stokes_jacobian(U)

    def _fixed_component_jacobian_apply_inverse(self, V, least_squares=False):
        assert not least_squares, "Least squares not implemented!"
        return self.solver.apply_inverse_stokes_jacobian(V)

    def restricted(self, dofs):
        restricted_op = CellModelRestrictedStokesOperator(self.solver, dofs)
        return restricted_op, restricted_op.source_dofs


class CellModel(Model):
    def __init__(
        self,
        t_end,
        dt,
        pfield_op,
        pfield_lin_op,
        ofield_op,
        stokes_op,
        initial_pfield,
        initial_ofield,
        initial_stokes,
        newton_params_pfield=NEWTON_PARAMS,
        newton_params_ofield=NEWTON_PARAMS,
        newton_params_stokes=NEWTON_PARAMS,
        least_squares_pfield=False,
        least_squares_ofield=False,
        least_squares_stokes=False,
        name=None,
        products={"pfield": None, "ofield": None, "stokes": None},
    ):
        self.__auto_init(locals())
        # self.t_end = t_end
        # self.dt = dt
        # self.pfield_op = pfield_op
        # self.ofield_op = ofield_op
        # self.stokes_op = stokes_op
        # self.initial_pfield = initial_pfield
        # self.initial_ofield = initial_ofield
        # self.initial_stokes = initial_stokes
        # self.newton_params_pfield = newton_params_pfield
        # self.newton_params_ofield = newton_params_ofield
        # self.newton_params_stokes = newton_params_stokes
        # self.least_squares_pfield = least_squares_pfield
        # self.least_squares_ofield = least_squares_ofield
        # self.least_squares_stokes = least_squares_stokes
        # self.name = name
        self.solution_space = BlockVectorSpace(
            [pfield_op.source.subspaces[0], ofield_op.source.subspaces[0], stokes_op.source.subspaces[0]]
        )
        self.linear = False
        self.dim_input = 0
        self._compute_allowed_kwargs = frozenset({"return_stages", "return_residuals"})
        self.initial_values = self.solution_space.make_array(
            [self.initial_pfield.as_vector(), self.initial_ofield.as_vector(), self.initial_stokes.as_vector()]
        )

    def _compute_solution(self, mu=None, **kwargs):
        return_stages = kwargs["return_stages"] if "return_stages" in kwargs else False
        return_residuals = kwargs["return_residuals"] if "return_residuals" in kwargs else False
        # initial values
        pfield_vecarray = self.initial_pfield.as_vector()
        ofield_vecarray = self.initial_ofield.as_vector()
        stokes_vecarray = self.initial_stokes.as_vector()
        U_all = self.solution_space.make_array([pfield_vecarray, ofield_vecarray, stokes_vecarray])

        # we do not have stages or residuals for the initial values, so we use empty lists
        pfield_stages = [pfield_vecarray.empty()]
        ofield_stages = [ofield_vecarray.empty()]
        stokes_stages = [stokes_vecarray.empty()]
        pfield_residuals = [pfield_vecarray.empty()]
        pfield_residuals_nonlin = [pfield_vecarray.empty()]
        ofield_residuals = [ofield_vecarray.empty()]
        stokes_residuals = [stokes_vecarray.empty()]

        i = 0
        t = 0.0
        # For the full-order model, we want to use the provided products in the newton scheme.
        # However, for the reduced model, the projected products are just the Euclidean products
        # (at least for the l2 and L2 products that we use) and, if we use DEIM, the range of
        # the operators is the space of DEIM coefficient, for which we do not have a custom product.
        # So for the reduced model, we want to use the Euclidean product both for source and range.
        # To be able to do that, we need to check if this method is called from the fom or the rom.
        is_fom = isinstance(self.pfield_op, CellModelPfieldOperator)
        while t < self.t_end - 1e-14:
            # match saving times and t_end_ exactly
            actual_dt = min(self.dt, self.t_end - t)
            # do a timestep
            print("Current time: {}".format(t), flush=True)
            # pfield_vecarray, pfield_data = newton(
            pfield_vecarray, pfield_data = newton_two_ops(
                # self.pfield_lin_op + self.pfield_op.fix_components((1, 2, 3), [pfield_vecarray, ofield_vecarray, stokes_vecarray]),
                self.pfield_lin_op,
                self.pfield_op.fix_components((1, 2, 3), [pfield_vecarray, ofield_vecarray, stokes_vecarray]),
                self.pfield_op.range.zeros(),  # pfield_op has same range as pfield_op with fixed components
                initial_guess=pfield_vecarray,
                mu=mu,
                least_squares=self.least_squares_pfield,
                return_stages=return_stages,
                return_residuals=return_residuals,
                source_product=self.products["pfield"] if is_fom else None,
                range_product=self.products["pfield"] if is_fom else None,
                **self.newton_params_pfield,
            )
            ofield_vecarray, ofield_data = newton(
                self.ofield_op.fix_components((1, 2, 3), [pfield_vecarray, ofield_vecarray, stokes_vecarray]),
                self.ofield_op.range.zeros(),
                initial_guess=ofield_vecarray,
                mu=mu,
                least_squares=self.least_squares_ofield,
                return_stages=return_stages,
                return_residuals=return_residuals,
                source_product=self.products["ofield"] if is_fom else None,
                range_product=self.products["ofield"] if is_fom else None,
                **self.newton_params_ofield,
            )
            stokes_vecarray, stokes_data = newton(
                self.stokes_op.fix_components((1, 2), [pfield_vecarray, ofield_vecarray]),
                self.stokes_op.range.zeros(),
                initial_guess=stokes_vecarray,
                mu=mu,
                least_squares=self.least_squares_stokes,
                return_stages=return_stages,
                return_residuals=return_residuals,
                source_product=self.products["stokes"] if is_fom else None,
                range_product=self.products["stokes"] if is_fom else None,
                **self.newton_params_stokes,
            )
            i += 1
            t += actual_dt
            U = self.solution_space.make_array([pfield_vecarray, ofield_vecarray, stokes_vecarray])
            U_all.append(U)

            if return_stages:
                pfield_stages.append(pfield_data["stages"])
                ofield_stages.append(ofield_data["stages"])
                stokes_stages.append(stokes_data["stages"])
            if return_residuals:
                pfield_residuals.append(pfield_data["residuals"])
                pfield_residuals_nonlin.append(pfield_data["residuals_nonlin"])
                ofield_residuals.append(ofield_data["residuals"])
                stokes_residuals.append(stokes_data["residuals"])

        retval = [U_all, {}]
        if return_stages:
            retval[1]["stages"] = (pfield_stages, ofield_stages, stokes_stages)
        if return_residuals:
            retval[1]["residuals"] = (pfield_residuals, ofield_residuals, stokes_residuals)
            retval[1]["residuals_nonlin"] = pfield_residuals_nonlin
        return retval

    def next_time_step(self, U_last, t, mu=None, return_output=False, return_stages=False, return_residuals=False):
        assert not return_output

        pfield_vecs, ofield_vecs, stokes_vecs = U_last._blocks

        if t > self.t_end - 1e-14:
            retval = [None, {"t": self.t_end}]
            if return_stages:
                retval[1]["stages"] = (None, None, None)
            if return_residuals:
                retval[1]["residuals"] = (None, None, None)
                retval[1]["residuals_nonlin"] = None
            return tuple(retval)

        # do not go past t_end
        actual_dt = min(self.dt, self.t_end - t)
        # see _compute_solution
        is_fom = isinstance(self.pfield_op, CellModelPfieldOperator)
        # do a timestep
        print("Current time: {}".format(t), flush=True)
        # pfield_vecs, pfield_data = newton(
        pfield_vecs, pfield_data = newton_two_ops(
            # self.pfield_lin_op + self.pfield_op.fix_components((1, 2, 3), [pfield_vecs, ofield_vecs, stokes_vecs]),
            self.pfield_lin_op,
            self.pfield_op.fix_components((1, 2, 3), [pfield_vecs, ofield_vecs, stokes_vecs]),
            self.pfield_op.range.zeros(),  # pfield_op has same range as the pfield_op with fixed components
            initial_guess=pfield_vecs,
            mu=mu,
            least_squares=self.least_squares_pfield,
            return_stages=return_stages,
            return_residuals=return_residuals,
            source_product=self.products["pfield"] if is_fom else None,
            range_product=self.products["pfield"] if is_fom else None,
            **self.newton_params_pfield,
        )
        ofield_vecs, ofield_data = newton(
            self.ofield_op.fix_components((1, 2, 3), [pfield_vecs, ofield_vecs, stokes_vecs]),
            self.ofield_op.range.zeros(),
            initial_guess=ofield_vecs,
            mu=mu,
            least_squares=self.least_squares_ofield,
            return_stages=return_stages,
            return_residuals=return_residuals,
            source_product=self.products["ofield"] if is_fom else None,
            range_product=self.products["ofield"] if is_fom else None,
            **self.newton_params_ofield,
        )
        stokes_vecs, stokes_data = newton(
            self.stokes_op.fix_components((1, 2), [pfield_vecs, ofield_vecs]),
            self.stokes_op.range.zeros(),
            initial_guess=stokes_vecs,
            mu=mu,
            least_squares=self.least_squares_stokes,
            return_stages=return_stages,
            return_residuals=return_residuals,
            source_product=self.products["stokes"] if is_fom else None,
            range_product=self.products["stokes"] if is_fom else None,
            **self.newton_params_stokes,
        )
        t += actual_dt
        U = self.solution_space.make_array([pfield_vecs, ofield_vecs, stokes_vecs])

        retval = [U, {"t": t}]
        if return_stages:
            retval[1]["stages"] = (pfield_data["stages"], ofield_data["stages"], stokes_data["stages"])
        if return_residuals:
            retval[1]["residuals"] = (pfield_data["residuals"], ofield_data["residuals"], stokes_data["residuals"])
            retval[1]["residuals_nonlin"] = pfield_data["residuals_nonlin"]
        return retval


class DuneCellModel(CellModel):
    def __init__(self, solver, products, name=None):
        pfield_op = CellModelPfieldOperator(solver, 0)
        pfield_lin_op = CellModelPfieldLinOperator(solver)
        ofield_op = CellModelOfieldOperator(solver, 0)
        stokes_op = CellModelStokesOperator(solver)
        initial_pfield = VectorOperator(solver.pfield_solution_space.make_array([solver.pfield_vector(0)]))
        initial_ofield = VectorOperator(solver.ofield_solution_space.make_array([solver.ofield_vector(0)]))
        initial_stokes = VectorOperator(solver.stokes_solution_space.make_array([solver.stokes_vector()]))
        self.dt = solver.dt
        self.t_end = solver.t_end
        super().__init__(
            t_end=self.t_end,
            dt=self.dt,
            pfield_op=pfield_op,
            pfield_lin_op=pfield_lin_op,
            ofield_op=ofield_op,
            stokes_op=stokes_op,
            initial_pfield=initial_pfield,
            initial_ofield=initial_ofield,
            initial_stokes=initial_stokes,
            newton_params_pfield=NEWTON_PARAMS,
            newton_params_ofield=NEWTON_PARAMS,
            newton_params_stokes=NEWTON_PARAMS,
            name=name,
            products=products,
        )
        self.parameters_own = CELLMODEL_PARAMETER_TYPE
        self.solver = solver

    def visualize(self, U, prefix="cellmodel_result", subsampling=True, every_nth=None):
        assert U in self.solution_space
        for i in range(len(U)):
            if every_nth is None or i % every_nth == 0:
                self.solver.set_pfield_vec(0, U._blocks[0]._list[i])
                self.solver.set_ofield_vec(0, U._blocks[1]._list[i])
                self.solver.set_stokes_vec(U._blocks[2]._list[i])
                step = i if every_nth is None else i // every_nth
                self.solver.visualize(prefix, step, step, subsampling)


class ProjectedSystemOperator(Operator):
    def __init__(self, operator, range_bases, source_bases):
        if range_bases is None:
            self.blocked_range_basis = False
            self.range = operator.range
        elif isinstance(range_bases, VectorArray):
            assert range_bases in operator.range
            range_bases = range_bases.copy()
            self.blocked_range_basis = False
            self.range = NumpyVectorSpace(len(range_bases))
        else:
            assert len(range_bases) == len(operator.range.subspaces)
            assert all(rb in rs for rb, rs in zip(range_bases, operator.range.subspaces))
            range_bases = tuple(rb.copy() for rb in range_bases)
            self.blocked_range_basis = True
            self.range = BlockVectorSpace([NumpyVectorSpace(len(rb)) for rb in range_bases])

        if source_bases is None:
            self.blocked_source_basis = False
            self.source = operator.source
        elif isinstance(source_bases, VectorArray):
            assert source_bases in operator.source
            source_bases = source_bases.copy()
            self.blocked_source_basis = False
            self.source = NumpyVectorSpace(len(source_bases))
        else:
            assert len(source_bases) == len(operator.source.subspaces)
            assert all(sb is None or sb in ss for sb, ss in zip(source_bases, operator.source.subspaces))
            source_bases = tuple(None if sb is None else sb.copy() for sb in source_bases)
            self.blocked_source_basis = True
            self.source = BlockVectorSpace(
                [
                    ss if sb is None else NumpyVectorSpace(len(sb))
                    for ss, sb in zip(operator.source.subspaces, source_bases)
                ]
            )

        self.__auto_init(locals())
        self.linear = operator.linear

    def apply(self, U, mu=None):
        raise NotImplementedError

    def fix_components(self, idx, U):
        if isinstance(idx, Number):
            idx = (idx,)
        if isinstance(U, VectorArray):
            U = (U,)
        assert len(idx) == len(U)
        assert all(len(u) == 1 for u in U)
        if not self.blocked_source_basis:
            raise NotImplementedError
        U = tuple(
            self.source_bases[i].lincomb(u.to_numpy()) if self.source_bases[i] is not None else u
            for i, u in zip(idx, U)
        )
        op = self.operator.fix_components(idx, U)
        if self.blocked_range_basis:
            raise NotImplementedError
        remaining_source_bases = [sb for i, sb in enumerate(self.source_bases) if i not in idx]
        if len(remaining_source_bases) != 1:
            raise NotImplementedError
        return ProjectedOperator(op, self.range_bases, remaining_source_bases[0])


class EmpiricalInterpolatedOperatorWithFixComponent(EmpiricalInterpolatedOperator):
    def fix_components(self, idx, U):
        return FixedComponentEmpiricalInterpolatedOperator(self, idx, U)


class FixedComponentEmpiricalInterpolatedOperator(EmpiricalInterpolatedOperator):
    def __init__(self, ei_operator, idx, U):
        assert isinstance(ei_operator, EmpiricalInterpolatedOperator)
        self.__auto_init(locals())
        # copy attributes from ei_operator
        self.interpolation_dofs = ei_operator.interpolation_dofs
        self.range = ei_operator.range
        self.linear = ei_operator.linear
        self.parameters = ei_operator.parameters
        self.solver_options = ei_operator.solver_options
        self.triangular = ei_operator.triangular
        self.name = f"{ei_operator.name}_fixed_component"

        # we only have to change the operators, source and source_dofs
        if hasattr(ei_operator, "restricted_operator"):
            self.interpolation_matrix = ei_operator.interpolation_matrix
            self.collateral_basis = ei_operator.collateral_basis
            restricted_op = ei_operator.restricted_operator
            subspaces = restricted_op.source.subspaces
            if idx != restricted_op.mutable_state_index:
                raise NotImplementedError
            U_restricted = [subspaces[j].from_numpy(U[i].dofs(ei_operator.source_dofs[j])) for i, j in enumerate(idx)]
            self.restricted_operator = restricted_op.fix_components(idx, U_restricted)
            fixed_source_indices = [i for i in range(len(subspaces)) if i not in idx]
            if len(fixed_source_indices) != 1:
                raise NotImplementedError
            self.source_dofs = ei_operator.source_dofs[fixed_source_indices[0]]
            # TODO: Replace next line, performs high-dimensional operations
            self._fixed_component_operator = ei_operator.operator.fix_components(idx, U)
            self._operator = weakref.ref(self._fixed_component_operator)
            self.source = self.operator.source
        else:
            self._operator = ei_operator.operator.fix_components(idx, U)
            self.source = self.operator.source


class ProjectedEmpiricalInterpolatedOperatorWithFixComponents(Operator):
    """A projected |EmpiricalInterpolatedOperator|."""

    def __init__(
        self,
        restricted_operator,
        interpolation_matrix,
        source_basis_dofs,
        projected_collateral_basis,
        triangular,
        solver_options=None,
        name=None,
    ):

        name = name or f"{restricted_operator.name}_projected"

        self.__auto_init(locals())
        self.source = BlockVectorSpace(NumpyVectorSpace(len(sbd)) for sbd in source_basis_dofs)
        self.range = projected_collateral_basis.space
        self.linear = restricted_operator.linear

    def apply(self, U, mu=None):
        raise NotImplementedError

    def jacobian(self, U, mu=None):
        raise NotImplementedError

    def fix_components(self, idx, U):
        restricted_op = self.restricted_operator
        if idx != restricted_op.mutable_state_index:
            raise NotImplementedError
        U_dofs = [self.source_basis_dofs[j].lincomb(U[i].to_numpy()) for i, j in enumerate(idx)]
        fixed_restricted_op = restricted_op.fix_components(idx, U_dofs)
        fixed_source_indices = [i for i in range(len(self.source.subspaces)) if i not in idx]
        if len(fixed_source_indices) != 1:
            raise NotImplementedError
        source_basis_dofs = self.source_basis_dofs[fixed_source_indices[0]]
        return ProjectedEmpiricalInterpolatedOperator(
            fixed_restricted_op,
            self.interpolation_matrix,
            source_basis_dofs,
            self.projected_collateral_basis,
            self.triangular,
            self.solver_options,
            f"{self.name}_fixed_component",
        )


class CellModelReductor(ProjectionBasedReductor):
    def __init__(
        self,
        fom,
        pfield_basis,
        ofield_basis,
        stokes_basis,
        check_orthonormality=None,
        check_tol=None,
        least_squares_pfield=True,
        least_squares_ofield=True,
        least_squares_stokes=True,
        pfield_deim_basis=None,
        ofield_deim_basis=None,
        stokes_deim_basis=None,
        products={"pfield": None, "ofield": None, "stokes": None},
    ):
        bases = {"pfield": pfield_basis, "ofield": ofield_basis, "stokes": stokes_basis}
        super().__init__(fom, bases, check_orthonormality=check_orthonormality, check_tol=check_tol, products=products)
        self.__auto_init(locals())

    reduce = ProjectionBasedReductor._reduce  # hack to allow bases which are None

    # copied from ProjectionBasedReductor, added the `if U is None` check
    def _check_orthonormality(self, basis, offset=0):
        if not self.check_orthonormality or basis not in self.products:
            return

        U = self.bases[basis]
        if U is None:
            return
        product = self.products.get(basis, None)
        error_matrix = U[offset:].inner(U, product)
        error_matrix[: len(U) - offset, offset:] -= np.eye(len(U) - offset)
        if error_matrix.size > 0:
            err = np.max(np.abs(error_matrix))
            if err >= self.check_tol:
                raise AccuracyError(f"result not orthogonal (max err={err})")

    def project_operators(self):
        fom = self.fom
        pfield_basis, ofield_basis, stokes_basis = (self.bases["pfield"], self.bases["ofield"], self.bases["stokes"])
        pfield_op, ofield_op, stokes_op = fom.pfield_op, fom.ofield_op, fom.stokes_op
        pfield_lin_op = fom.pfield_lin_op
        if self.pfield_deim_basis:
            pfield_dofs, pfield_deim_basis, _ = deim(self.pfield_deim_basis, pod=False, product=self.products["pfield"])
            pfield_op = EmpiricalInterpolatedOperatorWithFixComponent(pfield_op, pfield_dofs, pfield_deim_basis, False)
            projected_collateral_basis = (
                # If the full-order residual formulation is R(U) = 0, then
                # the Galerkin DEIM reduced model is
                #   V^T W Z (E^T Z)^{-1} E^T R(Vu) = 0,
                # where V in R^{Nxn} and Z in R^{Nxl} have the POD and DEIM basis as their columns, respectively,
                # E = (e_{k_1}, ..., e_{k_l}) in R^{Nxl} contains the unit vectors corresponding to the DEIM indices
                # and W is the positive definite matrix which defines the inner product.
                # If we split up the operator and only approximate the nonlinear part via DEIM, R(U) + R_lin U = 0, then
                # the Galerkin DEIM reduced model is
                #  V^T W R_lin V u + V^T W Z (E^T Z)^{-1} E^T R(Vu) = 0,
                # The pfield_deim_basis.inner(pfield_basis) part below computes (V^T W Z)^T.
                # E^T Z is stored in pfield_op.interpolation_matrix.
                NumpyVectorSpace.make_array(pfield_deim_basis.inner(pfield_basis, product=self.products["pfield"]))
                if not self.least_squares_pfield
                else
                # If we use least squares projection, we do not project via V^T, but we solve
                # argmin_u ||R_lin V u + Z (E^T Z)^{-1} E^T R(Vu)||_W
                # where W is the positive definite matrix which defines the inner product.
                # Since
                #   argmin_u ||R_lin V u + Z (E^T Z)^{-1} E^T R(Vu)||_W
                # = argmin_u ||R_lin V u + Z (E^T Z)^{-1} E^T R(Vu)||_W^2
                # = argmin_u [R_lin V u + Z (E^T Z)^{-1} E^T R(Vu)]^T W [R_lin V u + Z (E^T Z)^{-1} E^T R(Vu)]
                # = argmin_u R(Vu)^T E (E^T Z)^{-T} Z^T W Z (E^T Z)^{-1} E^T R(Vu) + u^T V^T R_lin^T W R_lin V u + u^T V^T R_lin^T W Z (E^T Z)^{-1} E^T R(Vu) + R(Vu)^T E (E^T Z)^{-T} Z^T W R_lin V u
                # = argmin_u R(Vu)^T E (E^T Z)^{-T} (E^T Z)^{-1} E^T R(Vu) + u^T V^T R_lin W R_lin V u + 2 u^T V^T R_lin^T W Z (E^T Z)^{-1} E^T R(Vu)
                # = argmin_u ||(E^T Z)^{-1} E^T R(Vu)||^2 + u^T X u + 2 u^T Y (E^T Z)^{-1} E^T R(Vu)
                # with X = V^T R_lin^T W R_lin V in R^{nxn} and Y = V^T R_lin^T W Z in R^{nxl} where we assumed that the DEIM basis is
                # W-orthonormal, i.e., Z^T W Z = I.
                # The argument can be evaluated efficiently since X and Y can be precomputed and to compute (E^T Z)^{-1} E^T R(Vu)
                # we only need the components of the nonlinear operator corresponding to the DEIM indices.
                # we can compute the minimum in the space of DEIM coefficients (i.e., project the collateral_basis
                # to its own spanned space which gives the unit matrix).
                # NumpyVectorSpace.make_array(np.eye(len(pfield_deim_basis)))  # assumes that pfield_deim_basis is ONB!!!
                NumpyVectorSpace.make_array(pfield_deim_basis.to_numpy())
            )
            # R_lin_V = pfield_lin_op.apply(pfield_basis)
            # X should be computed in the projection of pfield_lin_op below
            # X = R_lin_V.inner(R_lin_V, product=self.products["pfield"])
            # Y = R_lin_V.inner(pfield_deim_basis, product=self.products["pfield"])
            # Extract dofs for the reduced operator
            source_basis_dofs = []
            bases = [pfield_basis, pfield_basis, ofield_basis, stokes_basis]
            full_order_spaces = [
                fom.solver.pfield_solution_space,
                fom.solver.pfield_solution_space,
                fom.solver.ofield_solution_space,
                fom.solver.stokes_solution_space,
            ]
            for i, (basis, space) in enumerate(zip(bases, full_order_spaces)):
                source_dof_indices = pfield_op.source_dofs[i]
                if basis is not None:
                    dofs = basis.dofs(source_dof_indices)
                else:
                    # If we do not have a reduced basis, basis is None here but in theory
                    # should consists of all unit vectors of the full-dimensional space.
                    # We thus extract the dofs from these unit vectors.
                    dofs = np.zeros((space.dim, len(source_dof_indices)))
                    for j, dof in enumerate(source_dof_indices):
                        dofs[dof, j] = 1
                source_basis_dofs.append(NumpyVectorSpace.make_array(dofs))
            # Now create reduced operator
            pfield_op = ProjectedEmpiricalInterpolatedOperatorWithFixComponents(
                pfield_op.restricted_operator,
                pfield_op.interpolation_matrix,
                source_basis_dofs,
                projected_collateral_basis,
                False,
            )
        else:
            pfield_op = ProjectedSystemOperator(
                pfield_op,
                pfield_basis if not self.least_squares_pfield else None,
                [pfield_basis, pfield_basis, ofield_basis, stokes_basis],
            )
        if self.ofield_deim_basis:
            ofield_dofs, ofield_deim_basis, _ = deim(self.ofield_deim_basis, pod=False, product=self.products["ofield"])
            ofield_op = EmpiricalInterpolatedOperatorWithFixComponent(ofield_op, ofield_dofs, ofield_deim_basis, False)
            projected_collateral_basis = (
                NumpyVectorSpace.make_array(np.eye(len(ofield_deim_basis)))  # assumes that ofield_deim_basis is ONB!!!
                if self.least_squares_ofield
                else NumpyVectorSpace.make_array(ofield_deim_basis.inner(ofield_basis, product=products["ofield"]))
            )
            # Extract dofs for the reduced operator
            source_basis_dofs = []
            bases = [ofield_basis, pfield_basis, ofield_basis, stokes_basis]
            full_order_spaces = [
                fom.solver.ofield_solution_space,
                fom.solver.pfield_solution_space,
                fom.solver.ofield_solution_space,
                fom.solver.stokes_solution_space,
            ]
            for i, (basis, space) in enumerate(zip(bases, full_order_spaces)):
                source_dof_indices = ofield_op.source_dofs[i]
                if basis is not None:
                    dofs = basis.dofs(source_dof_indices)
                else:
                    dofs = np.zeros((space.dim, len(source_dof_indices)))
                    for j, dof in enumerate(source_dof_indices):
                        dofs[dof, j] = 1
                source_basis_dofs.append(NumpyVectorSpace.make_array(dofs))
            # Now create reduced operator
            ofield_op = ProjectedEmpiricalInterpolatedOperatorWithFixComponents(
                ofield_op.restricted_operator,
                ofield_op.interpolation_matrix,
                source_basis_dofs,
                projected_collateral_basis,
                False,
            )
        else:
            ofield_op = ProjectedSystemOperator(
                ofield_op,
                ofield_basis if not self.least_squares_ofield else None,
                [ofield_basis, pfield_basis, ofield_basis, stokes_basis],
            )
        if self.stokes_deim_basis:
            stokes_dofs, stokes_deim_basis, _ = deim(self.stokes_deim_basis, pod=False, product=self.products["stokes"])
            stokes_op = EmpiricalInterpolatedOperatorWithFixComponent(stokes_op, stokes_dofs, stokes_deim_basis, False)
            projected_collateral_basis = (
                NumpyVectorSpace.make_array(np.eye(len(stokes_deim_basis)))  # assumes that stokes_deim_basis is ONB!!!
                if self.least_squares_stokes
                else NumpyVectorSpace.make_array(stokes_deim_basis.inner(stokes_basis, product=self.products["stokes"]))
            )
            # Extract dofs for the reduced operator
            source_basis_dofs = []
            bases = [stokes_basis, pfield_basis, ofield_basis]
            full_order_spaces = [
                fom.solver.stokes_solution_space,
                fom.solver.pfield_solution_space,
                fom.solver.ofield_solution_space,
            ]
            for i, (basis, space) in enumerate(zip(bases, full_order_spaces)):
                source_dof_indices = stokes_op.source_dofs[i]
                if basis is not None:
                    dofs = basis.dofs(source_dof_indices)
                else:
                    dofs = np.zeros((space.dim, len(source_dof_indices)))
                    for j, dof in enumerate(source_dof_indices):
                        dofs[dof, j] = 1
                source_basis_dofs.append(NumpyVectorSpace.make_array(dofs))
            # Now create reduced operator
            stokes_op = ProjectedEmpiricalInterpolatedOperatorWithFixComponents(
                stokes_op.restricted_operator,
                stokes_op.interpolation_matrix,
                source_basis_dofs,
                projected_collateral_basis,
                False,
            )
        else:
            stokes_op = ProjectedSystemOperator(
                stokes_op,
                stokes_basis if not self.least_squares_stokes else None,
                [stokes_basis, pfield_basis, ofield_basis],
            )
        # This is a VectorArrayOperator: pfield_basis_size -> N, where N is the FE-space dimension
        projected_pfield_lin_op = project(fom.pfield_lin_op, None, pfield_basis, product=fom.products["pfield"])
        # Since the projected pfield_op is a NumpyMatrixOperator, we have to convert the projected pfield_lin_op to be compatible.
        pfield_lin_op = NumpyMatrixOperator(projected_pfield_lin_op.array.to_numpy().T)
        projected_operators = {
            "pfield_op": pfield_op,
            "pfield_lin_op": pfield_lin_op,
            "ofield_op": ofield_op,
            "stokes_op": stokes_op,
            "initial_pfield": project(fom.initial_pfield, pfield_basis, None, product=fom.products["pfield"]),
            "initial_ofield": project(fom.initial_ofield, ofield_basis, None, product=fom.products["ofield"]),
            "initial_stokes": project(fom.initial_stokes, stokes_basis, None, product=fom.products["stokes"]),
        }
        return projected_operators

    def project_operators_to_subbasis(self, dims):
        raise NotImplementedError

    def build_rom(self, projected_operators, estimator):
        return self.fom.with_(
            new_type=CellModel,
            least_squares_pfield=self.least_squares_pfield,
            least_squares_ofield=self.least_squares_ofield,
            least_squares_stokes=self.least_squares_stokes,
            newton_params_pfield=NEWTON_PARAMS,
            newton_params_ofield=NEWTON_PARAMS,
            newton_params_stokes=NEWTON_PARAMS,
            **projected_operators,
        )

    def reconstruct(self, u, basis="RB"):
        if basis != "RB":
            raise NotImplementedError
        ret = []
        for i, s in enumerate(["pfield", "ofield", "stokes"]):
            basis_s = self.bases[s]
            u_i = u._blocks[i]
            ret.append(basis_s.lincomb(u_i.to_numpy()) if basis_s is not None else u_i)
        return self.fom.solution_space.make_array(ret)


def calculate_cellmodel_trajectory_errors(
    modes,
    deim_modes,
    testcase,
    t_end,
    dt,
    grid_size_x,
    grid_size_y,
    pol_order,
    mu,
    u_rom,
    reductor,
    products,
    pickled_data_available,
    num_chunks,
    pickle_prefix,
):
    proj_errs = [0.0] * len(modes)
    red_errs = [0.0] * len(modes)
    rel_red_errs = [0.0] * len(modes)
    proj_deim_errs = [0.0] * len(deim_modes)
    nonlin_proj_deim_errs = 0.0
    norms = [[], [], [], [], [], []]
    # modes has length 2*num_cells+1
    num_cells = (len(modes) - 1) // 2
    assert num_cells == 1, "Not tested for more than 1 cell, most likely there are errors!"
    n = 0
    n_deim = [0] * 3
    t = 0.0
    timestep_residuals = None
    if not pickled_data_available:
        solver = CellModelSolver(testcase, t_end, dt, grid_size_x, grid_size_y, pol_order, mu)
        cellmodel = DuneCellModel(
            solver, products={"pfield": products[0], "ofield": products[1], "stokes": products[2]}
        )
        current_values = cellmodel.initial_values.copy()
        while True:
            # POD basis errors
            U_rom = reductor.reconstruct(u_rom[n])
            for i in range(3):
                vals = current_values.block(i)
                # projection errors
                proj_res = vals - modes[i].lincomb(vals.inner(modes[i], product=products[i]))
                proj_errs[i] += np.sum(proj_res.norm2(product=products[i]))
                # model reduction errors
                res = vals - U_rom.block(i)
                res_norms2 = res.norm2(product=products[i])
                red_errs[i] += np.sum(res_norms2)
                # relative model reduction errors
                vals_norms2 = vals.norm2(product=products[i])
                rel_errs_list = [
                    res_norm2 / val_norm2
                    for res_norm2, val_norm2 in zip(res_norms2, vals_norms2)
                    if not np.isclose(val_norm2, 0)
                ]
                rel_red_errs[i] += np.sum(rel_errs_list)
            # DEIM basis errors
            if timestep_residuals is not None:
                for i in range(3):
                    deim_res = timestep_residuals[i]
                    if deim_modes[i] is not None:
                        proj_res = deim_res - deim_modes[i].lincomb(deim_res.inner(deim_modes[i], product=products[i]))
                        proj_deim_errs[i] += np.sum(proj_res.norm2(product=products[i]))
                        n_deim[i] += len(proj_res)
                nonlin_deim_res = data["residuals_nonlin"]
                nonlin_proj_res = nonlin_deim_res - deim_modes[i].lincomb(nonlin_deim_res.inner(deim_modes[i], product=products[i]))
                nonlin_proj_deim_errs += np.sum(nonlin_proj_res.norm2(product=products[i]))
            # get values for next time step
            n += 1
            current_values, data = cellmodel.next_time_step(
                current_values, t, mu=mu, return_stages=False, return_residuals=True
            )
            t = data["t"]
            timestep_residuals = data["residuals"]
            if current_values is None:
                break
        fom_time = None
    else:
        n = 0
        for chunk_index in range(num_chunks):
            with open(f"{pickle_prefix}{chunk_index}.pickle", "rb") as f:
                chunk_data = pickle.load(f)
            chunk_size = len(chunk_data["snaps"][0])
            U_rom = reductor.reconstruct(u_rom[n : n + chunk_size])
            # POD basis errors
            for i in range(3):
                vals = chunk_data["snaps"][i]
                # norms[i].extend(vals.norm(product=products[i]).tolist())
                # projection errors
                if modes[i] is not None:
                    proj_res = vals - modes[i].lincomb(vals.inner(modes[i], product=products[i]))
                    proj_errs[i] += np.sum(proj_res.norm2(product=products[i]))
                # model reduction errors
                res = vals - U_rom.block(i)
                res_norms2 = res.norm2(product=products[i])
                red_errs[i] += np.sum(res_norms2)
                # relative model reduction errors
                vals_norms2 = vals.norm2(product=products[i])
                rel_errs_list = [
                    res_norm2 / val_norm2
                    for res_norm2, val_norm2 in zip(res_norms2, vals_norms2)
                    if not np.isclose(val_norm2, 0)
                ]
                rel_red_errs[i] += np.sum(rel_errs_list)
            # DEIM basis errors
            for i in range(3):
                deim_res = chunk_data["residuals"][i]
                # deim_res = deim_res * np.array([1/norm if norm > 0 else 1 for norm in deim_res.norm(product=products[i])])
                # norms[3+i].extend(deim_res.norm(product=products[i]).tolist())
                if deim_modes[i] is not None:
                    proj_res = deim_res - deim_modes[i].lincomb(deim_res.inner(deim_modes[i], product=products[i]))
                    proj_deim_errs[i] += np.sum(proj_res.norm2(product=products[i]))
                    n_deim[i] += len(proj_res)
            n += chunk_size
        fom_time = chunk_data["elapsed"]
    return proj_errs, proj_deim_errs, nonlin_proj_deim_errs, red_errs, rel_red_errs, n, n_deim, fom_time, norms


def calculate_mean_cellmodel_projection_errors(
    modes,
    deim_modes,
    testcase,
    t_end,
    dt,
    grid_size_x,
    grid_size_y,
    pol_order,
    mus,
    reduced_us,
    reductor,
    mpi_wrapper,
    products,
    pickled_data_available,
    num_chunks,
    pickle_prefix,
    rom_time,
):
    proj_errs_sum = [0] * 3
    proj_deim_errs_sum = [0] * 3
    red_errs_sum = [0] * 3
    rel_red_errs_sum = [0] * 3
    num_residuals = [0] * 3
    num_snapshots = 0
    mean_fom_time = 0.0
    norms = 0
    mu_to_red_errs = dict()
    for mu, u_rom in zip(mus, reduced_us):
        pickle_prefix_mu = f"{pickle_prefix}_Be{mu['Be']}_Ca{mu['Ca']}_Pa{mu['Pa']}_chunk"
        (
            proj_errs,
            proj_deim_errs,
            nonlin_proj_deim_errs,
            red_errs,
            rel_red_errs,
            n,
            n_deim,
            fom_time,
            norms,
        ) = calculate_cellmodel_trajectory_errors(
            modes=modes,
            deim_modes=deim_modes,
            testcase=testcase,
            t_end=t_end,
            dt=dt,
            grid_size_x=grid_size_x,
            grid_size_y=grid_size_y,
            pol_order=pol_order,
            mu=mu,
            u_rom=u_rom,
            reductor=reductor,
            products=products,
            pickled_data_available=pickled_data_available,
            num_chunks=num_chunks,
            pickle_prefix=pickle_prefix_mu,
        )
        mu_as_str = f"Be: {mu['Be']:.3f}, Ca: {mu['Ca']:.3f}, Pa: {mu['Pa']:.3f}"
        mu_to_red_errs[mu_as_str] = red_errs
        for i in range(3):
            proj_errs_sum[i] += proj_errs[i]
            proj_deim_errs_sum[i] += proj_deim_errs[i]
            red_errs_sum[i] += red_errs[i]
            rel_red_errs_sum[i] += rel_red_errs[i]
            num_residuals[i] += n_deim[i]
        nonlin_proj_deim_errs_sum += nonlin_proj_deim_errs
        num_snapshots += n
        if fom_time is not None:
            mean_fom_time += fom_time
        else:
            mean_fom_time = None
    mean_fom_time = mean_fom_time / len(mus) if mean_fom_time is not None else None
    proj_errs = [0.0] * len(modes)
    proj_deim_errs = proj_errs.copy()
    nonlin_proj_deim_errs = 0.0
    red_errs = [0.0] * len(modes)
    rel_red_errs = [0.0] * len(modes)
    num_snapshots = mpi_wrapper.comm_world.gather(num_snapshots, root=0)
    if fom_time is not None:
        fom_time = mpi_wrapper.comm_world.gather(fom_time, root=0)
    if rom_time is not None:
        rom_time = mpi_wrapper.comm_world.gather(rom_time, root=0)
    # num_residuals_list is a list of lists. The inner lists have length 3 and contain the number of residuals for each field on that rank
    num_residuals_list = mpi_wrapper.comm_world.gather(num_residuals, root=0)
    mean_rom_time = None
    if mpi_wrapper.rank_world == 0:
        num_snapshots = np.sum(num_snapshots)
        mean_fom_time = mean(fom_time) if mean_fom_time is not None else None
        mean_rom_time = mean(rom_time) if rom_time is not None else None
        num_residuals = [0] * 3
        for i in range(3):
            num_residuals[i] = sum(nums[i] for nums in num_residuals_list)
    mu_to_red_errs_gathered = mpi_wrapper.comm_world.gather(mu_to_red_errs, root=0)
    all_mu_to_red_errs = dict()
    if mpi_wrapper.rank_world == 0:
        for d in mu_to_red_errs_gathered:
            all_mu_to_red_errs.update(d)
    for index, err in enumerate(proj_errs_sum):
        err = mpi_wrapper.comm_world.gather(err, root=0)
        if mpi_wrapper.rank_world == 0:
            proj_errs[index] = np.sqrt(np.sum(err) / num_snapshots)
    for index, red_err in enumerate(red_errs_sum):
        red_err = mpi_wrapper.comm_world.gather(red_err, root=0)
        if mpi_wrapper.rank_world == 0:
            red_errs[index] = np.sqrt(np.sum(red_err) / num_snapshots)
    for index, rel_red_err in enumerate(rel_red_errs_sum):
        rel_red_err = mpi_wrapper.comm_world.gather(rel_red_err, root=0)
        if mpi_wrapper.rank_world == 0:
            rel_red_errs[index] = np.sqrt(np.sum(rel_red_err) / num_snapshots)
    for i in range(3):
        err = mpi_wrapper.comm_world.gather(proj_deim_errs_sum[i], root=0)
        if mpi_wrapper.rank_world == 0:
            proj_deim_errs[i] = np.sqrt(np.sum(err) / num_residuals[i]) if num_residuals[i] != 0 else 0
    err = mpi_wrapper.comm_world.gather(nonlin_proj_deim_errs_sum, root=0)
    if mpi_wrapper.rank_world == 0:
        nonlin_proj_deim_errs = np.sqrt(np.sum(err) / num_residuals[0]) if num_residuals[0] != 0 else 0
    return proj_errs, proj_deim_errs, nonlin_proj_deim_errs, red_errs, rel_red_errs, mean_fom_time, mean_rom_time, norms, all_mu_to_red_errs


def calculate_cellmodel_errors(
    modes,
    deim_modes,
    testcase,
    t_end,
    dt,
    grid_size_x,
    grid_size_y,
    pol_order,
    mus,
    reduced_us,
    reductor,
    mpi_wrapper,
    logfile_name=None,
    prefix="",
    products=[None] * 3,
    pickled_data_available=False,
    num_chunks=0,
    pickle_prefix=None,
    rom_time=None,
):
    """Calculates projection error. As we cannot store all snapshots due to memory restrictions, the
    problem is solved again and the error calculated on the fly"""
    start = timer()
    (
        errs,
        deim_errs,
        nonlin_deim_errs,
        red_errs,
        rel_red_errs,
        mean_fom_time,
        mean_rom_time,
        norms,
        mus_to_red_errs,
    ) = calculate_mean_cellmodel_projection_errors(
        modes=modes,
        deim_modes=deim_modes,
        testcase=testcase,
        t_end=t_end,
        dt=dt,
        grid_size_x=grid_size_x,
        grid_size_y=grid_size_y,
        pol_order=pol_order,
        mus=mus,
        reduced_us=reduced_us,
        reductor=reductor,
        mpi_wrapper=mpi_wrapper,
        products=products,
        pickled_data_available=pickled_data_available,
        num_chunks=num_chunks,
        pickle_prefix=pickle_prefix,
        rom_time=rom_time,
    )
    elapsed = timer() - start
    if mpi_wrapper.rank_world == 0 and logfile_name is not None:
        with open(logfile_name, "a") as logfile:
            logfile.write("Time used for calculating error: " + str(elapsed) + "\n")
            nc = (len(modes) - 1) // 2
            error_type = "l2"
            if isinstance(products[0], CellModelPfieldH1ProductOperator):
                error_type = "H1"
            elif isinstance(products[0], CellModelPfieldL2ProductOperator):
                error_type = "L2"
            for k in range(nc):
                logfile.write("{}{} projection error for {}-th pfield is: {}\n".format(prefix, error_type, k, errs[k]))
                logfile.write(
                    "{}{} projection error for {}-th ofield is: {}\n".format(prefix, error_type, k, errs[nc + k])
                )
            logfile.write("{}{} projection error for stokes is: {}\n".format(prefix, error_type, errs[2 * nc]))
            logfile.write(
                "{}{} projection DEIM error for {}-th pfield is: {}\n".format(prefix, error_type, 0, deim_errs[0])
            )
            logfile.write(
                "{}{} nonlin projection DEIM error for {}-th pfield is: {}\n".format(prefix, error_type, 0, nonlin_deim_errs)
            )
            logfile.write(
                "{}{} projection DEIM error for {}-th ofield is: {}\n".format(prefix, error_type, 0, deim_errs[1])
            )
            logfile.write("{}{} projection DEIM error for stokes is: {}\n".format(prefix, error_type, deim_errs[2]))
            for k in range(nc):
                logfile.write(
                    "{}{} reduction error for {}-th pfield is: {}\n".format(prefix, error_type, k, red_errs[k])
                )
                logfile.write(
                    "{}{} reduction error for {}-th ofield is: {}\n".format(prefix, error_type, k, red_errs[nc + k])
                )
            logfile.write(
                "{}{} reduction error for {}-th stokes is: {}\n".format(prefix, error_type, 0, red_errs[2 * nc])
            )
            for k in range(nc):
                logfile.write(
                    "{}{} relative reduction error for {}-th pfield is: {}\n".format(
                        prefix, error_type, k, rel_red_errs[k]
                    )
                )
                logfile.write(
                    "{}{} relative reduction error for {}-th ofield is: {}\n".format(
                        prefix, error_type, k, rel_red_errs[nc + k]
                    )
                )
            logfile.write(
                "{}{} relative reduction error for {}-th stokes is: {}\n".format(
                    prefix, error_type, 0, rel_red_errs[2 * nc]
                )
            )
            if mean_fom_time is not None and mean_rom_time is not None:
                logfile.write(f"{mean_fom_time:.2f} vs. {mean_rom_time:.2f}, speedup {mean_fom_time/mean_rom_time:.2f}")
            for mu, red_err in mus_to_red_errs.items():
                logfile.write(f"{mu}: ")
                for err in red_err:
                    logfile.write(f"{err:.2e} ")
                logfile.write("\n")
            # logfile.write("{}L2 reduction error for {}-th ofield is: {}\n".format(prefix, k, red_errs[nc + k]))
            # logfile.write("{}L2 reduction error for stokes is: {}\n".format(prefix, red_errs[2 * nc]))
        # for i, filename in enumerate(("norms_pfield_40.txt", "norms_ofield_40.txt", "norms_stokes_40.txt", "norms_pfield_deim_40.txt", "norms_ofield_deim_40.txt", "norms_stokes_deim_40.txt")):
        #     with open(filename, "w") as f:
        #         f.write("index norm\n")
        #         for index, norm in enumerate(norms[i]):
        #             f.write(f"{index} {norm}\n")
    return errs


def get_num_chunks_and_num_timesteps(t_end, dt, chunk_size):
    num_time_steps = math.ceil(t_end / dt) + 1.0
    num_chunks = int(math.ceil(num_time_steps / chunk_size))
    last_chunk_size = num_time_steps - chunk_size * (num_chunks - 1)
    assert num_chunks >= 2
    assert 1 <= last_chunk_size <= chunk_size
    return num_chunks, num_time_steps


def create_parameters(
    train_params_per_rank,
    test_params_per_rank,
    rf,
    mpi,
    excluded_params,
    filename=None,
    Be0=1.0,
    Ca0=1.0,
    Pa0=1.0,
    sampling_type="uniform",
):
    # check excluded_params
    for param in excluded_params:
        if param not in ("Be", "Ca", "Pa"):
            raise NotImplementedError(f"Unknown excluded parameter {param}")
    # Be0, Ca0 and Pa0 are default values for parameters
    ####### Create training parameters ######
    num_train_params = train_params_per_rank * mpi.size_world
    num_test_params = test_params_per_rank * mpi.size_world
    # We have three parameters minus the excluded ones and want to sample all of these parameters with the same number of values
    num_sampled_parameters = 3 - len(excluded_params)
    values_per_sampled_parameter = int(num_train_params ** (1 / num_sampled_parameters))
    rf = np.sqrt(rf)
    lower_bound_Be = Be0 / rf
    upper_bound_Be = Be0 * rf
    lower_bound_Ca = Ca0 / rf
    upper_bound_Ca = Ca0 * rf
    lower_bound_Pa = Pa0 / rf
    upper_bound_Pa = Pa0 * rf
    if sampling_type == "uniform":
        # uniform distribution of the parameter
        Be_values = (
            np.linspace(lower_bound_Be, upper_bound_Be, values_per_sampled_parameter).tolist()
            if "Be" not in excluded_params
            else [Be0]
        )
        Ca_values = (
            np.linspace(lower_bound_Ca, upper_bound_Ca, values_per_sampled_parameter).tolist()
            if "Ca" not in excluded_params
            else [Pa0]
        )
        Pa_values = (
            np.linspace(lower_bound_Pa, upper_bound_Pa, values_per_sampled_parameter).tolist()
            if "Pa" not in excluded_params
            else [Pa0]
        )
    elif sampling_type == "uniform_reciprocal":
        # uniform distribution of 1/parameter
        Be_values = (
            np.reciprocal(np.linspace(1 / upper_bound_Be, 1 / lower_bound_Be, values_per_sampled_parameter)).tolist()
            if "Be" not in excluded_params
            else [Be0]
        )
        Ca_values = (
            np.reciprocal(np.linspace(1 / upper_bound_Ca, 1 / lower_bound_Ca, values_per_sampled_parameter)).tolist()
            if "Ca" not in excluded_params
            else [Ca0]
        )
        Pa_values = (
            np.reciprocal(np.linspace(1 / upper_bound_Pa, 1 / lower_bound_Pa, values_per_sampled_parameter)).tolist()
            if "Pa" not in excluded_params
            else [Pa0]
        )
    elif sampling_type == "log":
        # log distribution of parameter
        # same as log distribution of 1/parameter as long as we are using 1 / rf, 1 * rf as bounds
        Be_values = (
            np.logspace(
                math.log(lower_bound_Be, 10), math.log(upper_bound_Be, 10), values_per_sampled_parameter
            ).tolist()
            if "Be" not in excluded_params
            else [Be0]
        )
        Ca_values = (
            np.logspace(
                math.log(lower_bound_Ca, 10), math.log(upper_bound_Ca, 10), values_per_sampled_parameter
            ).tolist()
            if "Ca" not in excluded_params
            else [Ca0]
        )
        Pa_values = (
            np.logspace(
                math.log(lower_bound_Pa, 10), math.log(upper_bound_Pa, 10), values_per_sampled_parameter
            ).tolist()
            if "Pa" not in excluded_params
            else [Pa0]
        )
    elif sampling_type == "log_inverted":
        # Choose parameters such that the final distribution of 1/param looks like a log distribution,
        # but with density increasing towards the upper bound, not the lower bound
        Be_log_values = np.logspace(
            math.log(lower_bound_Be, 10), math.log(upper_bound_Be, 10), values_per_sampled_parameter
        )
        Be_values = (
            1 / (1 / upper_bound_Be + 1 / lower_bound_Be - 1 / Be_log_values) if "Be" not in excluded_params else [Be0]
        )
        Ca_log_values = np.logspace(
            math.log(lower_bound_Ca, 10), math.log(upper_bound_Ca, 10), values_per_sampled_parameter
        )
        Ca_values = (
            1 / (1 / upper_bound_Ca + 1 / lower_bound_Ca - 1 / Ca_log_values) if "Ca" not in excluded_params else [Ca0]
        )
        Pa_log_values = np.logspace(
            math.log(lower_bound_Pa, 10), math.log(upper_bound_Pa, 10), values_per_sampled_parameter
        )
        Pa_values = (
            1 / (1 / upper_bound_Pa + 1 / lower_bound_Pa - 1 / Pa_log_values) if "Pa" not in excluded_params else [Pa0]
        )
    elif sampling_type == "log_and_log_inverted":
        # Choose half of the parameters according to log and half according to log_inverted sampling type.
        # This gives a distribution that has a higher density towards the boundaries of the parameter interval.
        vals_per_param = values_per_sampled_parameter // 2
        remaining_vals_per_param = values_per_sampled_parameter - vals_per_param
        Be_values1 = np.logspace(
            math.log(lower_bound_Be, 10), math.log(upper_bound_Be, 10), vals_per_param + 1
        ).tolist()
        Be_log_values = np.logspace(
            math.log(lower_bound_Be, 10), math.log(upper_bound_Be, 10), remaining_vals_per_param + 1
        )
        Be_values2 = (1 / (1 / upper_bound_Be + 1 / lower_bound_Be - 1 / Be_log_values)).tolist()
        # if mpi.rank_world == 0:
        # print(Be_values1, flush=True)
        # print(Be_values2, flush=True)
        Be_values2 = Be_values2[1:-1]
        Be_values = Be_values1 + Be_values2 if "Be" not in excluded_params else [Be0]
        Ca_values1 = np.logspace(
            math.log(lower_bound_Ca, 10), math.log(upper_bound_Ca, 10), vals_per_param + 1
        ).tolist()
        Ca_log_values = np.logspace(
            math.log(lower_bound_Ca, 10), math.log(upper_bound_Ca, 10), remaining_vals_per_param + 1
        )
        Ca_values2 = (1 / (1 / upper_bound_Ca + 1 / lower_bound_Ca - 1 / Ca_log_values)).tolist()
        Ca_values2 = Ca_values2[1:-1]
        Ca_values = Ca_values1 + Ca_values2 if "Ca" not in excluded_params else [Ca0]
        Pa_values1 = np.logspace(
            math.log(lower_bound_Pa, 10), math.log(upper_bound_Pa, 10), vals_per_param + 1
        ).tolist()
        Pa_log_values = np.logspace(
            math.log(lower_bound_Pa, 10), math.log(upper_bound_Pa, 10), remaining_vals_per_param + 1
        )
        Pa_values2 = (1 / (1 / upper_bound_Pa + 1 / lower_bound_Pa - 1 / Pa_log_values)).tolist()
        Pa_values2 = Pa_values2[1:-1]
        Pa_values = Pa_values1 + Pa_values2 if "Pa" not in excluded_params else [Pa0]
        if mpi.rank_world == 0:
            print(Be_values, flush=True)
            print(Ca_values, flush=True)
            print(Pa_values, flush=True)
    else:
        raise NotImplementedError("Unknown sampling_type")
    mus = []
    for Be in Be_values:
        for Ca in Ca_values:
            for Pa in Pa_values:
                mus.append({"Pa": Pa, "Be": Be, "Ca": Ca})
    while len(mus) < num_train_params:
        mus.append(
            {
                "Be": Be0 if "Be" in excluded_params else random.uniform(lower_bound_Be, upper_bound_Be),
                "Ca": Ca0 if "Ca" in excluded_params else random.uniform(lower_bound_Ca, upper_bound_Ca),
                "Pa": Pa0 if "Pa" in excluded_params else random.uniform(lower_bound_Pa, upper_bound_Pa),
            }
        )
    ####### Create test parameters ########
    new_mus = []
    for _ in range(num_test_params):
        new_mus.append(
            {
                "Be": Be0 if "Be" in excluded_params else random.uniform(lower_bound_Be, upper_bound_Be),
                "Ca": Ca0 if "Ca" in excluded_params else random.uniform(lower_bound_Ca, upper_bound_Ca),
                "Pa": Pa0 if "Pa" in excluded_params else random.uniform(lower_bound_Pa, upper_bound_Pa),
            }
        )
    ############ write mus to file #################
    if filename is not None:
        if mpi.rank_world == 0:
            with open(filename, "w") as ff:
                ff.write(
                    f"{filename}\nTrained with {len(mus)} Parameters: {mus}\n"
                    f"Tested with {len(new_mus)} new Parameters: {new_mus}\n"
                )

    ####### Visualize parameters ##########
    # if mpi.rank_world == 0:
    #     y = np.zeros(num_train_params)
    #     plt.plot([mu["Be"] for mu in mus], y + 0.55, "o", label="Be")
    #     plt.plot([1 / mu["Be"] for mu in mus], y + 0.45, "o", label="1/Be")
    #     plt.plot([mu["Ca"] for mu in mus], y + 0.05, "o", label="Ca")
    #     plt.plot([1 / mu["Ca"] for mu in mus], y - 0.05, "o", label="1/Ca")
    #     plt.plot([mu["Pa"] for mu in mus], y - 0.45, "o", label="Pa")
    #     plt.plot([1 / mu["Pa"] for mu in mus], y - 0.55, "o", label="1/Pa")
    #     plt.legend()
    #     plt.show()
    #     y = np.zeros(num_test_params)
    #     plt.plot([1 / mu["Be"] for mu in new_mus], y + 0.5, "o")
    #     plt.plot([1 / mu["Ca"] for mu in new_mus], y + 0.0, "o")
    #     plt.plot([1 / mu["Pa"] for mu in new_mus], y - 0.5, "o")
    #     plt.show()
    ####### Scatter parameters to MPI ranks #######
    # Transform mus and new_mus from plain list to list of lists where the i-th inner list contains all parameters for rank i
    mus = np.reshape(np.array(mus), (mpi.size_world, train_params_per_rank)).tolist()
    new_mus = np.reshape(np.array(new_mus), (mpi.size_world, test_params_per_rank)).tolist()
    mus = mpi.comm_world.scatter(mus, root=0)
    print(f"Mu on rank {mpi.rank_world}: {mus}")
    new_mus = mpi.comm_world.scatter(new_mus, root=0)
    return mus, new_mus


def solver_statistics(t_end: float, dt: float, chunk_size: int):
    num_time_steps = math.ceil(t_end / dt) + 1.0
    num_chunks = int(math.ceil(num_time_steps / chunk_size))
    last_chunk_size = num_time_steps - chunk_size * (num_chunks - 1)
    assert num_chunks >= 2
    assert 1 <= last_chunk_size <= chunk_size
    return num_chunks, num_time_steps


class BinaryTreeHapodResults:
    def __init__(self, tree_depth, epsilon_ast, omega):
        self.params = HapodParameters(tree_depth, epsilon_ast=epsilon_ast, omega=omega)
        self.modes: Union[ListVectorArray, NumpyVectorArray, None] = None
        self.svals = None
        self.num_modes: Union[list[int], int, None] = 0
        self.max_local_modes: Union[list[int], int, None] = 0
        self.max_vectors_before_pod: Union[list[int], int, None] = 0
        self.total_num_snapshots: Union[list[int], int, None] = 0
        self.orth_tol: float = 0.0
        self.final_orth_tol: float = 0.0
        self.gathered_modes = None
        self.num_snaps = 0
        self.timings = {}
        self.incremental_gramian = False
        self.win: Any = None


# Performs a POD on each processor core with the data vectors computed on that core,
# then sends resulting (scaled) modes to rank 0 on that processor.
# The resulting modes are stored in results.gathered_modes on processor rank 0
# results.num_snaps (on rank 0) contains the total number of snapshots
# that have been processed (on all ranks)
def pods_on_processor_cores_in_binary_tree_hapod(r, vecs, mpi, root, product, method="method_of_snapshots"):
    assert isinstance(vecs, ListVectorArray)
    print("start processor pod: ", mpi.rank_world)
    snaps_on_rank = len(vecs)
    r.max_vectors_before_pod = max(r.max_vectors_before_pod, snaps_on_rank)
    vecs, svals = local_pod(
        [vecs], snaps_on_rank, r.params, product=product, incremental_gramian=False, orth_tol=r.orth_tol, method=method
    )
    r.max_local_modes = max(r.max_local_modes, len(vecs))
    vecs.scal(svals)
    idle_wait(mpi.comm_proc, root=root)
    r.gathered_modes, _, r.num_snaps, _ = mpi.comm_proc.gather_on_root_rank(
        vecs, num_snapshots_on_rank=snaps_on_rank, num_modes_equal=False, root=root
    )


# perform a POD with gathered modes on rank 0 on each processor/node
def pod_on_node_in_binary_tree_hapod(
    r, is_first_chunk, is_last_chunk, mpi, root, product, method="method_of_snapshots"
):
    print("start node pod: ", mpi.rank_world)
    r.total_num_snapshots += r.num_snaps
    if is_first_chunk:
        r.max_vectors_before_pod = max(r.max_vectors_before_pod, len(r.gathered_modes))
        r.modes, r.svals = local_pod(
            inputs=[r.gathered_modes],
            num_snaps_in_leafs=r.num_snaps,
            parameters=r.params,
            product=product,
            orth_tol=r.orth_tol,
            incremental_gramian=False,
            method=method,
        )
    else:
        r.max_vectors_before_pod = max(r.max_vectors_before_pod, len(r.modes) + len(r.gathered_modes))
        root_of_tree = is_last_chunk and mpi.size_rank_group[root] == 1
        r.modes, r.svals = local_pod(
            inputs=[[r.modes, r.svals], r.gathered_modes],
            num_snaps_in_leafs=r.total_num_snapshots,
            parameters=r.params,
            orth_tol=r.final_orth_tol if root_of_tree else r.orth_tol,
            incremental_gramian=r.incremental_gramian,
            product=product,
            root_of_tree=root_of_tree,
            method=method,
        )
    r.max_local_modes = max(r.max_local_modes, len(r.modes))


def final_hapod_in_binary_tree_hapod(r, mpi, root, product, method="method_of_snapshots"):
    print("start final pod: ", mpi.rank_world)
    (r.modes, r.svals, r.total_num_snapshots, max_num_input_vecs, max_num_local_modes) = binary_tree_hapod_over_ranks(
        mpi.comm_rank_group[root],
        r.modes,
        r.total_num_snapshots,
        r.params,
        svals=r.svals,
        last_hapod=True,
        incremental_gramian=r.incremental_gramian,
        product=product,
        orth_tol=r.final_orth_tol,
        method=method,
    )
    r.max_vectors_before_pod = max(r.max_vectors_before_pod, max_num_input_vecs)
    r.max_local_modes = max(r.max_local_modes, max_num_local_modes)


def binary_tree_hapod(
    chunk_generator,
    mpi: MPIWrapper,
    tolerances: "list[float]",
    indices: "list[int]",
    omega: float = 0.95,
    incremental_gramian: bool = False,
    orth_tol: float = np.inf,
    # orth_tol=1e-10,
    final_orth_tol: float = 1e-10,
    logfile: Union[None, str] = None,
    products: "list[Any]" = [None] * 6,
    pod_method: str = "method_of_snapshots",
):
    assert len(tolerances) == 6

    # setup timings
    timings = {}
    for k in indices:
        timings[f"POD{k}"] = 0.0

    # calculate rooted tree depth
    node_binary_tree_depth = binary_tree_depth(mpi.comm_rank_group[0])  # assumes same tree for all fields
    node_binary_tree_depth = mpi.comm_proc.bcast(node_binary_tree_depth, root=0)
    tree_depth = chunk_generator.num_chunks + node_binary_tree_depth

    # create classes that store HAPOD results and parameters for easier handling
    results = [BinaryTreeHapodResults(tree_depth, tol, omega) for tol in tolerances]
    # add some more properties to the results classes
    for r in results:
        r.orth_tol = orth_tol
        r.final_orth_tol = final_orth_tol
        r.incremental_gramian = incremental_gramian

    # walk over time chunks
    for chunk in chunk_generator:
        # calculate POD of timestep vectors on each core
        for k in indices:
            t1 = timer()
            root_rank = k % mpi.size_proc
            pods_on_processor_cores_in_binary_tree_hapod(
                results[k], chunk[k], mpi, root=root_rank, product=products[k], method=pod_method
            )
            timings[f"POD{k}"] += timer() - t1
        for k in indices:
            t1 = timer()
            # perform PODs in parallel for each field
            root_rank = k % mpi.size_proc
            if mpi.rank_proc == root_rank:
                # perform pod on rank root_rank with gathered modes and modes from the last chunk
                pod_on_node_in_binary_tree_hapod(
                    results[k],
                    is_first_chunk=chunk_generator.chunk_index() == 0,
                    is_last_chunk=chunk_generator.done(),
                    mpi=mpi,
                    root=root_rank,
                    product=products[k],
                    method=pod_method,
                )
            timings[f"POD{k}"] += timer() - t1

    # Finally, perform a HAPOD over a binary tree of nodes
    for k in indices:
        root_rank = k % mpi.size_proc
        r = results[k]
        if mpi.rank_proc == root_rank:
            t1 = timer()
            final_hapod_in_binary_tree_hapod(r, mpi, root=root_rank, product=products[k], method=pod_method)
            timings[f"POD{k}"] += timer() - t1

        # calculate max number of local modes
        # The 'or [0]' is only here to silence pyright which otherwise complains below that we cannot apply max to None
        r.max_vectors_before_pod = mpi.comm_world.gather(r.max_vectors_before_pod, root=0) or [0]
        r.max_local_modes = mpi.comm_world.gather(r.max_local_modes, root=0) or [0]
        r.num_modes = mpi.comm_world.gather(len(r.modes) if r.modes is not None else 0, root=0) or [0]
        r.total_num_snapshots = mpi.comm_world.gather(r.total_num_snapshots, root=0) or [0]
        gathered_timings = mpi.comm_world.gather(timings, root=0) or [0]
        if mpi.rank_world == 0:
            r.max_vectors_before_pod = max(r.max_vectors_before_pod)
            r.max_local_modes = max(r.max_local_modes)
            r.num_modes = max(r.num_modes)
            r.total_num_snapshots = max(r.total_num_snapshots)
            r.timings = {}
            for key in timings.keys():
                r.timings[key] = max([timing[key] for timing in gathered_timings])

    # write statistics to file
    if logfile is not None and mpi.rank_world == 0:
        with open(logfile, "a") as ff:
            for k in indices:
                r = results[k]
                ff.write(f"Hapod for index {k}\n")
                ff.write(
                    f"The HAPOD resulted in {r.num_modes} final modes taken from a total of {r.total_num_snapshots} snapshots!\n"
                )
                ff.write(f"The maximal number of local modes was {r.max_local_modes}\n")
                ff.write(f"The maximal number of input vectors to a local POD was: {r.max_vectors_before_pod}\n")
                ff.write("PODs took {} s.\n".format(r.timings[f"POD{k}"]))
            ff.write(
                f"The maximum amount of memory used on rank 0 was: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1000.0 ** 2} GB\n"
            )
    mpi.comm_world.Barrier()
    return results

