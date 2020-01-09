import math
import numpy as np
import random
from timeit import default_timer as timer
import weakref

from pymor.algorithms.projection import project
from pymor.core.interfaces import abstractmethod
from pymor.models.basic import ModelBase
from pymor.operators.basic import OperatorBase
from pymor.operators.constructions import (VectorOperator, ConstantOperator, LincombOperator, LinearOperator,
                                           FixedParameterOperator)
from pymor.parameters.base import Parameter, ParameterType, Parametric
from pymor.parameters.functionals import ExpressionParameterFunctional
from pymor.parameters.spaces import CubicParameterSpace
from pymor.reductors.basic import ProjectionBasedReductor
from pymor.vectorarrays.block import BlockVectorSpace
from pymor.vectorarrays.list import VectorInterface, ListVectorSpace, ListVectorArray
from pymor.vectorarrays.numpy import NumpyVectorArray, NumpyVectorSpace

import libhapodgdt
from libhapodgdt import CommonDenseVector

#import cvxopt
#from cvxopt import matrix as cvxmatrix

IMPL_TYPES = (CommonDenseVector,)

PARAMETER_TYPE = ParameterType({'s': (4,)})


class Solver(Parametric):

    def __init__(self, *args):
        self.impl = libhapodgdt.BoltzmannSolver2d(*args)
        #self.impl = libhapodgdt.BoltzmannSolver3d(*args)
        self.last_mu = None
        self.solution_space = DuneXtLaListVectorSpace(self.impl.get_initial_values().dim)
        self.build_parameter_type(PARAMETER_TYPE)

    def linear(self):
        return self.impl.linear()

    def solve(self, with_half_steps=True):
        return self.solution_space.make_array(self.impl.solve(with_half_steps))

    def next_n_timesteps(self, n, with_half_steps=True):
        return self.solution_space.make_array(self.impl.next_n_timesteps(n, with_half_steps))

    def reset(self):
        self.impl.reset()

    def finished(self):
        return self.impl.finished()

    def current_time(self):
        return self.impl.current_time()

    def t_end(self):
        return self.impl.t_end()

    def set_current_time(self, time):
        return self.impl.set_current_time(time)

    def set_current_solution(self, vec):
        return self.impl.set_current_solution(vec)

    def time_step_length(self):
        return self.impl.time_step_length()

    def get_initial_values(self):
        return self.solution_space.make_array([self.impl.get_initial_values()])

    def set_rhs_operator_params(self,
                                sigma_s_scattering=1,
                                sigma_s_absorbing=0,
                                sigma_t_scattering=1,
                                sigma_t_absorbing=10):
        mu = (sigma_s_scattering, sigma_s_absorbing, sigma_t_scattering, sigma_t_absorbing)
        if mu != self.last_mu:
            self.last_mu = mu
            self.impl.set_rhs_operator_parameters(*mu)

    def set_rhs_timestepper_params(self,
                                   sigma_s_scattering=1,
                                   sigma_s_absorbing=0,
                                   sigma_t_scattering=1,
                                   sigma_t_absorbing=10):
        mu = (sigma_s_scattering, sigma_s_absorbing, sigma_t_scattering, sigma_t_absorbing)
        if mu != self.last_mu:
            self.last_mu = mu
            self.impl.set_rhs_timestepper_parameters(*mu)


class BoltzmannModelBase(ModelBase):

    def __init__(self,
                 lf,
                 rhs,
                 initial_data,
                 nt=60,
                 dt=0.056,
                 t_end=3.2,
                 operators=None,
                 products=None,
                 estimator=None,
                 visualizer=None,
                 parameter_space=None,
                 cache_region=None,
                 name=None):
        super().__init__(products=products,
                         estimator=estimator,
                         visualizer=visualizer,
                         cache_region=cache_region,
                         name=name)
        self.build_parameter_type(PARAMETER_TYPE)
        self.__auto_init(locals())
        self.solution_space = self.initial_data.range

    # def project_to_realizable_set(self, vec, cvxopt_P, cvxopt_G, cvxopt_h, dim, space):
    #    cvxopt_q = cvxmatrix(-vec.to_numpy().transpose(), size=(dim,1), tc='d')
    #    sol = cvxopt.solvers.qp(P=cvxopt_P, q=cvxopt_q, G=cvxopt_G, h=cvxopt_h)
    #    if 'optimal' not in sol['status']:
    #        raise NotImplementedError
    #    return NumpyVectorArray(np.array(sol['x']).reshape(1, dim), space)

    # def is_realizable(self, coeffs, basis):
    #    tol = 1e-8
    #    vec = basis.lincomb(coeffs._data)
    #    return np.all(np.greater_equal(vec._data, tol))

    def _solve(self,
               mu=None,
               return_output=False,
               return_half_steps=False,
               cvxopt_P=None,
               cvxopt_G=None,
               cvxopt_h=None,
               basis=None):
        assert not return_output
        U = self.initial_data.as_vector(mu)
        U_half = U.empty()
        U_last = U.copy()
        rhs = self.rhs.assemble(mu)
        final_dt = self.t_end - (self.nt - 1) * self.dt
        assert final_dt >= 0 and final_dt <= self.dt
        for n in range(self.nt):
            dt = self.dt if n != self.nt - 1 else final_dt
            self.logger.info('Time step {}'.format(n))
            param = Parameter({'t': n * self.dt, 'dt': self.dt})
            param['s'] = mu['s']
            V = U_last - self.lf.apply(U_last, param) * dt
            # if cvxopt_P is not None and not self.is_realizable(V, basis):
            #    V = self.project_to_realizable_set(V, cvxopt_P, cvxopt_G, cvxopt_h, V.dim, V.space)
            if return_half_steps:
                U_half.append(V)
            U_last = V + rhs.apply(V, mu=mu) * dt     # explicit Euler for RHS
            # if cvxopt_P is not None and not self.is_realizable(U_last, basis):
            #    U_last = self.project_to_realizable_set(U_last, cvxopt_P, cvxopt_G, cvxopt_h, V.dim, V.space)
            # matrix exponential for RHS
            # mu['dt'] = dt
            # U_last = rhs.apply(V, mu=mu)
            U.append(U_last)
        if return_half_steps:
            return U, U_half
        else:
            return U


class DuneModel(BoltzmannModelBase):

    def __init__(self, nt=60, dt=0.056, *args):
        self.solver = solver = Solver(*args)
        initial_data = VectorOperator(solver.get_initial_values())
        # lf_operator = LFOperator(self.solver)
        # Todo: rename from lf_operator to kinetic_operator
        lf_operator = KineticOperator(self.solver)
        self.non_decomp_rhs_operator = ndrhs = RHSOperator(self.solver)
        param = solver.parse_parameter([0., 0., 0., 0.])
        affine_part = ConstantOperator(ndrhs.apply(initial_data.range.zeros(), mu=param), initial_data.range)
        rhs_operator = affine_part + \
            LincombOperator(
                [LinearOperator(FixedParameterOperator(RHSOperator(self.solver),
                                                       solver.parse_parameter(mu=[0., 0., 0., 0.])) - affine_part),
                 LinearOperator(FixedParameterOperator(RHSOperator(self.solver),
                                                       solver.parse_parameter(mu=[1., 0., 0., 0.])) - affine_part),
                 LinearOperator(FixedParameterOperator(RHSOperator(self.solver),
                                                       solver.parse_parameter(mu=[0., 1., 0., 0.])) - affine_part),
                 LinearOperator(FixedParameterOperator(RHSOperator(self.solver),
                                                       solver.parse_parameter(mu=[1., 0., 1., 0.])) - affine_part),
                 LinearOperator(FixedParameterOperator(RHSOperator(self.solver),
                                                       solver.parse_parameter(mu=[0., 1., 0., 1.])) - affine_part)],
                [ExpressionParameterFunctional('1 - s[0] - s[1]', PARAMETER_TYPE),
                 ExpressionParameterFunctional('s[0] - s[2]', PARAMETER_TYPE),
                 ExpressionParameterFunctional('s[1] - s[3]', PARAMETER_TYPE),
                 ExpressionParameterFunctional('s[2]', PARAMETER_TYPE),
                 ExpressionParameterFunctional('s[3]', PARAMETER_TYPE)]
            )
        param_space = CubicParameterSpace(PARAMETER_TYPE, 0., 10.)
        super().__init__(initial_data=initial_data,
                         lf=lf_operator,
                         rhs=rhs_operator,
                         t_end=solver.t_end(),
                         nt=nt,
                         dt=dt,
                         parameter_space=param_space,
                         name='DuneModel')

    def _solve(self, mu=None, return_output=False, return_half_steps=False):
        assert not return_output
        return (self.with_(new_type=BoltzmannModelBase,
                           rhs=self.non_decomp_rhs_operator).solve(mu=mu, return_half_steps=return_half_steps))


class DuneOperatorBase(OperatorBase):

    def __init__(self, solver):
        self.solver = solver
        self.linear = solver.linear()
        self.source = self.range = solver.solution_space
        self.dt = solver.time_step_length()


class RestrictedDuneOperatorBase(OperatorBase):

    def __init__(self, solver, source_dim, range_dim):
        self.solver = solver
        self.source = NumpyVectorSpace(source_dim)
        self.range = NumpyVectorSpace(range_dim)
        self.dt = solver.time_step_length()


class LFOperator(DuneOperatorBase):

    linear = True

    def apply(self, U, mu=None):
        assert U in self.source
        return self.range.make_array([
            self.solver.impl.apply_LF_operator(u.impl, mu['t'] if mu is not None and 't' in mu else 0.,
                                               mu['dt'] if mu is not None and 'dt' in mu else self.dt) for u in U._list
        ])


class RestrictedKineticOperator(RestrictedDuneOperatorBase):

    linear = False

    def __init__(self, solver, dofs):
        self.solver = solver
        self.dofs = dofs
        dofs_as_list = [int(i) for i in dofs]
        self.solver.impl.prepare_restricted_operator(dofs_as_list)
        super(RestrictedKineticOperator, self).__init__(solver, self.solver.impl.len_source_dofs(), len(dofs))

    def apply(self, U, mu=None):
        assert U in self.source
        # hack to ensure realizability for hatfunction moment models
        for vec in U._data:
            vec[np.where(vec < 1e-8)] = 1e-8
        U = DuneXtLaListVectorSpace.from_numpy(U.to_numpy())
        ret = [
            DuneXtLaVector(self.solver.impl.apply_restricted_kinetic_operator(u.impl)).to_numpy(True) for u in U._list
        ]
        return self.range.make_array(ret)


class KineticOperator(DuneOperatorBase):

    def apply(self, U, mu=None):
        assert U in self.source
        if not self.linear:
            # hack to ensure realizability for hatfunction moment models
            for vec in U._data:
                vec[np.where(vec < 1e-8)] = 1e-8
        return self.range.make_array([
            self.solver.impl.apply_kinetic_operator(u.impl,
                                                    float(mu['t']) if mu is not None and 't' in mu else 0.,
                                                    float(mu['dt']) if mu is not None and 'dt' in mu else self.dt)
            for u in U._list
        ])

    def restricted(self, dofs):
        return RestrictedKineticOperator(self.solver, dofs), np.array(self.solver.impl.source_dofs())


class GodunovOperator(DuneOperatorBase):

    linear = True

    def apply(self, U, mu=None):
        assert U in self.source
        return self.range.make_array([self.solver.impl.apply_godunov_operator(u.impl, 0.) for u in U._list])


class RHSOperator(DuneOperatorBase):

    linear = True

    def __init__(self, solver):
        super(RHSOperator, self).__init__(solver)
        self.build_parameter_type(PARAMETER_TYPE)

    def apply(self, U, mu=None):
        assert U in self.source
        # explicit euler for rhs
        self.solver.set_rhs_operator_params(*map(float, mu['s']))
        return self.range.make_array([self.solver.impl.apply_rhs_operator(u.impl, 0.) for u in U._list])
        # matrix exponential for rhs
        # return self.range.make_array([self.solver.impl.apply_rhs_timestepper(u.impl, 0., mu['dt'][0]) for u in U._list])
        # self.solver.set_rhs_timestepper_params(*map(float, mu['s']))


class DuneXtLaVector(VectorInterface):

    def __init__(self, impl):
        self.impl = impl
        self.dim = impl.dim

    def to_numpy(self, ensure_copy=False):
        # if ensure_copy:
            return np.frombuffer(self.impl.buffer(), dtype=np.double).copy()
        # else:
        #    return np.frombuffer(self.impl.buffer(), dtype=np.double)

    @classmethod
    def make_zeros(cls, subtype):
        impl = subtype[0](subtype[1], 0.)
        return DuneXtLaVector(impl)

    @property
    def subtype(self):
        return (type(self.impl), self.impl.dim)

    @property
    def data(self):
        return np.frombuffer(self.impl.buffer(), dtype=np.double)

    def copy(self, deep=False):
        return DuneXtLaVector(self.impl.copy())

    def scal(self, alpha):
        self.impl.scal(alpha)

    def axpy(self, alpha, x):
        self.impl.axpy(alpha, x.impl)

    def dot(self, other):
        return self.impl.dot(other.impl)

    def l1_norm(self):
        return self.impl.l1_norm()

    def l2_norm(self):
        return self.impl.l2_norm()

    def l2_norm2(self):
        return self.impl.l2_norm()

    def sup_norm(self):
        return self.impl.sup_norm()

    def dofs(self, dof_indices):
        assert 0 <= np.min(dof_indices)
        assert np.max(dof_indices) < self.dim
        return np.array([self.impl[int(i)] for i in dof_indices])

    def amax(self):
        return self.impl.amax()

    def __add__(self, other):
        return DuneXtLaVector(self.impl + other.impl)

    def __iadd__(self, other):
        self.impl += other.impl
        return self

    __radd__ = __add__

    def __sub__(self, other):
        return DuneXtLaVector(self.impl - other.impl)

    def __isub__(self, other):
        self.impl -= other.impl
        return self

    def __mul__(self, other):
        return DuneXtLaVector(self.impl * other)

    def __neg__(self):
        return DuneXtLaVector(-self.impl)

    def __getstate__(self):
        return type(self.impl), self.data

    def __setstate__(self, state):
        self.impl = state[0](len(state[1]), 0.)
        self.data[:] = state[1]


class DuneXtLaListVectorSpace(ListVectorSpace):

    def __init__(self, dim, id=None):
        self.dim = dim
        self.id = id

    def __eq__(self, other):
        return type(other) is DuneXtLaListVectorSpace and self.dim == other.dim and self.id == other.id

    @classmethod
    def space_from_vector_obj(cls, vec, id_):
        return cls(len(vec), id_)

    @classmethod
    def space_from_dim(cls, dim, id):
        return cls(dim, id)

    def zero_vector(self):
        return DuneXtLaVector(CommonDenseVector(self.dim, 0))

    def make_vector(self, obj):
        return DuneXtLaVector(obj)

    @classmethod
    def from_memory(cls, numpy_array):
        (num_vecs, dim) = numpy_array.shape
        vecs = []
        for i in range(num_vecs):
            vecs.append(DuneXtLaVector(CommonDenseVector.create_from_buffer(numpy_array.data, i * dim, dim)))
        space = DuneXtLaListVectorSpace(dim)
        return ListVectorArray(vecs, space)

    def vector_from_numpy(self, data, ensure_copy=False):
        # TODO: do not copy if ensure_copy is False
        v = self.zero_vector()
        v.data[:] = data
        return v


class BoltzmannRBReductor(ProjectionBasedReductor):

    def __init__(self, fom, RB=None, check_orthonormality=None, check_tol=None):
        assert isinstance(fom, BoltzmannModelBase)
        RB = fom.solution_space.empty() if RB is None else RB
        assert RB in fom.solution_space, (RB.space, fom.solution_space)
        super().__init__(fom, {'RB': RB}, check_orthonormality=check_orthonormality, check_tol=check_tol)

    def project_operators(self):
        fom = self.fom
        RB = self.bases['RB']
        projected_operators = {
            'lf': project(fom.lf, RB, RB),
            'rhs': project(fom.rhs, RB, RB),
            'initial_data': project(fom.initial_data, RB, None),
            'products': {k: project(v, RB, RB) for k, v in fom.products.items()},
        }
        return projected_operators

    def project_operators_to_subbasis(self, dims):
        rom = self._last_rom
        dim = dims['RB']
        projected_operators = {
            'lf': project_to_subbasis(rom.lf, dim, dim),
            'rhs': project_to_subbasis(rom.rhs, dim, dim),
            'initial_data': project_to_subbasis(rom.initial_data, dim, None),
            'products': {k: project_to_subbasis(v, dim, dim) for k, v in rom.products.items()},
        }
        return projected_operators

    def build_rom(self, projected_operators, estimator):
        return self.fom.with_(new_type=BoltzmannModelBase, **projected_operators)


############################### CellModel ######################################################

CELLMODEL_PARAMETER_TYPE = ParameterType({'s': (3,)})


class CellModelSolver(Parametric):

    def __init__(self, testcase, t_end, grid_size_x, grid_size_y, mu):
        self.impl = libhapodgdt.CellModelSolver(testcase, t_end, grid_size_x, grid_size_y, False, *mu)
        self.last_mu = mu
        self.pfield_solution_space = DuneXtLaListVectorSpace(self.impl.pfield_vec(0).dim)
        self.pfield_numpy_space = NumpyVectorSpace(self.impl.pfield_vec(0).dim)
        self.ofield_solution_space = DuneXtLaListVectorSpace(self.impl.ofield_vec(0).dim)
        self.stokes_solution_space = DuneXtLaListVectorSpace(self.impl.stokes_vec().dim)
        self.build_parameter_type(PARAMETER_TYPE)
        self.num_cells = self.impl.num_cells()

    def linear(self):
        return self.impl.linear()

    def solve(self, dt, write=False, write_step=0, filename='', subsampling=True):
        return self.dune_result_to_pymor(self.impl.solve(dt, write, write_step, filename, subsampling))

    def next_n_timesteps(self, n, dt):
        return self.dune_result_to_pymor(self.impl.next_n_timesteps(n, dt))

    def dune_result_to_pymor(self, result):
        ret = []
        nc = self.num_cells
        for k in range(nc):
            ret.append(self.pfield_solution_space.make_array(result[k]))
        for k in range(nc):
            ret.append(self.ofield_solution_space.make_array(result[nc + k]))
        ret.append(self.stokes_solution_space.make_array(result[2 * nc]))
        return ret

    # def reset(self):
    #     self.impl.reset()
    def visualize(self, prefix, num, dt, subsampling=True):
        self.impl.visualize(prefix, num, dt, subsampling)

    def finished(self):
        return self.impl.finished()

    def apply_pfield_product_operator(self, U, mu=None, numpy=False):
        pfield_space = self.pfield_solution_space
        if not numpy:
            U_out = [self.impl.apply_pfield_product_op(vec.impl) for vec in U._list]
            return pfield_space.make_array(U_out)
        else:
            U_list = pfield_space.make_array([pfield_space.vector_from_numpy(vec).impl for vec in U.to_numpy()])
            U_out = [DuneXtLaVector(self.impl.apply_pfield_product_op(vec.impl)).to_numpy(True) for vec in U_list._list]
            return self.pfield_numpy_space.make_array(U_out)

    def apply_ofield_product_operator(self, U, mu=None):
        U_out = [self.impl.apply_ofield_product_op(vec.impl) for vec in U._list]
        return self.ofield_solution_space.make_array(U_out)

    def apply_stokes_product_operator(self, U, mu=None):
        U_out = [self.impl.apply_stokes_product_op(vec.impl) for vec in U._list]
        return self.stokes_solution_space.make_array(U_out)

    def pfield_vector(self, cell_index):
        return DuneXtLaVector(self.impl.pfield_vec(cell_index))

    def ofield_vector(self, cell_index):
        return DuneXtLaVector(self.impl.ofield_vec(cell_index))

    def stokes_vector(self):
        return DuneXtLaVector(self.impl.stokes_vec())

    def set_pfield_vec(self, cell_index, vec):
        return self.impl.set_pfield_vec(cell_index, vec.impl)

    def set_ofield_vec(self, cell_index, vec):
        return self.impl.set_ofield_vec(cell_index, vec.impl)

    def set_stokes_vec(self, vec):
        return self.impl.set_stokes_vec(vec.impl)

    def prepare_pfield_operator(self, dt, cell_index):
        return self.impl.prepare_pfield_op(dt, cell_index)

    def prepare_ofield_operator(self, dt, cell_index):
        return self.impl.prepare_ofield_op(dt, cell_index)

    def prepare_stokes_operator(self):
        return self.impl.prepare_stokes_op()

    def apply_inverse_pfield_operator(self, vec, cell_index):
        return self.pfield_solution_space.make_array([self.impl.apply_inverse_pfield_op(vec.impl, cell_index)])

    def apply_inverse_ofield_operator(self, vec, cell_index):
        return self.ofield_solution_space.make_array([self.impl.apply_inverse_ofield_op(vec.impl, cell_index)])

    def apply_inverse_stokes_operator(self):
        return self.stokes_solution_space.make_array([self.impl.apply_inverse_stokes_op()])

    def apply_pfield_operator(self, U, cell_index, dt, mu=None):
        U_out = [self.impl.apply_pfield_op(vec.impl, cell_index) for vec in U._list]
        return self.pfield_solution_space.make_array(U_out)

    def apply_ofield_operator(self, U, cell_index, dt, mu=None):
        U_out = [self.impl.apply_ofield_op(vec.impl, cell_index) for vec in U._list]
        return self.ofield_solution_space.make_array(U_out)

    def apply_stokes_operator(self, U, mu=None):
        U_out = [self.impl.apply_stokes_op(vec.impl) for vec in U._list]
        return self.stokes_solution_space.make_array(U_out)


    # def current_time(self):
    #     return self.impl.current_time()

    # def t_end(self):
    #     return self.impl.t_end()

    # def set_current_time(self, time):
    #     return self.impl.set_current_time(time)

    # def set_current_solution(self, vec):
    #     return self.impl.set_current_solution(vec)

    # def time_step_length(self):
    #     return self.impl.time_step_length()

    # def get_initial_values(self):
    #      return self.solution_space.make_array([self.impl.get_initial_values()])


class CellModelPfieldProductOperator(OperatorBase):

    def __init__(self, solver):
        self.solver = solver
        self.linear = True

    def apply(self, U, mu=None, numpy=False):
        return self.solver.apply_pfield_product_operator(U, numpy=numpy)


class CellModelOfieldProductOperator(OperatorBase):

    def __init__(self, solver):
        self.solver = solver
        self.linear = True

    def apply(self, U, mu=None):
        return self.solver.apply_ofield_product_operator(U)


class CellModelStokesProductOperator(OperatorBase):

    def __init__(self, solver):
        self.solver = solver
        self.linear = True

    def apply(self, U, mu=None):
        return self.solver.apply_stokes_product_operator(U)


class FixedComponentWrapperOperator(OperatorBase):

    def __init__(self, operator, idx, value):
        value = value.copy()
        self.__auto_init(locals())
        self._fixed_component_operator = None
        op = self._get_operator()
        self.source = op.source
        self.range = op.range
        self.linear = op.linear

    def _get_operator(self):
        if self._fixed_component_operator is None:
            op = None
        else:
            op = self._fixed_component_operator()

        if op is None:
            op = self.operator._fix_component(self.idx, self.value)
            self._fixed_component_operator = weakref.ref(op)

        return op

    def apply(self, U, mu=None):
        op = self._get_operator()
        return op.apply(U, mu=mu)

    def apply_inverse(self, V, mu=None, least_squares=False):
        op = self._get_operator()
        return op.apply_inverse(V, mu=mu, least_squares=least_squares)

    def jacobian(self, U, mu=None):
        op = self._get_operator()
        return op.jacobian(U, mu=mu)


class MutableStateOperator(OperatorBase):

    mutable_state_index = 1

    def apply(self, U, mu):
        assert U in self.source
        op = self.fix_component(self.mutable_state_index, U._blocks[self.mutable_state_index])
        U = U._blocks[:self.mutable_state_index] + U._blocks[self.mutable_state_index+1:]
        if len(U) > 1:
            U = op.source.make_array(U)
        return op.apply(U, mu=mu)

    @abstractmethod
    def _set_state(self, U):
        pass

    def _fix_component(self, idx, U):
        if idx != self.mutable_state_index:
            raise NotImplementedError
        # store a strong ref to the current fixed component operator
        # FixedComponentWrapperOperators storing a weakref to an older instance
        # will loose their operators and will have to call this method again
        operator = self._set_state(U)
        self._last_fixed_component_operator = operator
        return operator

    def fix_component(self, idx, U):
        assert len(U) == 1
        return FixedComponentWrapperOperator(self, idx, U)


class MutableStateCellModelOperator(MutableStateOperator):

    def _fix_component(self, idx, U):
        if idx != self.mutable_state_index:
            raise NotImplementedError
        operator = self._set_state(U)
        self.solver._last_fixed_component_operator = operator
        return operator


class CellModelPfieldFixedOperator(OperatorBase):

    def __init__(self, solver, cell_index, dt):
        self.__auto_init(locals())
        self.linear = False
        self.source = self.range = self.solver.pfield_solution_space

    def apply(self, U, mu=None):
        return self.solver.apply_pfield_operator(U, self.cell_index, self.dt)

    def apply_inverse(self, V, mu=None, least_squares=False):
        assert sum(V.norm()) == 0., "Not implemented for non-zero rhs!"
        assert not least_squares, "Least squares not implemented!"
        return self.solver.apply_inverse_pfield_operator(self.solver.pfield_vector(self.cell_index), self.cell_index)


class CellModelPfieldOperator(MutableStateCellModelOperator):

    def __init__(self, solver, cell_index, dt):
        self.__auto_init(locals())
        self.linear = False
        self.source = BlockVectorSpace([self.solver.pfield_solution_space,
                                        BlockVectorSpace([self.solver.pfield_solution_space,
                                                          self.solver.ofield_solution_space,
                                                          self.solver.stokes_solution_space])])
        self.range = self.solver.pfield_solution_space

    def _set_state(self, U):
        self.solver.set_pfield_vec(0, U._blocks[0]._list[0])
        self.solver.set_ofield_vec(0, U._blocks[1]._list[0])
        self.solver.set_stokes_vec(U._blocks[2]._list[0])
        self.solver.prepare_pfield_operator(self.dt, self.cell_index)
        return CellModelPfieldFixedOperator(self.solver, self.cell_index, self.dt)


class CellModelOfieldFixedOperator(OperatorBase):

    def __init__(self, solver, cell_index, dt):
        self.__auto_init(locals())
        self.linear = False
        self.source = self.range = self.solver.ofield_solution_space

    def apply(self, U, mu=None):
        return self.solver.apply_ofield_operator(U, self.cell_index, self.dt)

    def apply_inverse(self, V, mu=None, least_squares=False):
        assert sum(V.norm()) == 0., "Not implemented for non-zero rhs!"
        assert not least_squares, "Least squares not implemented!"
        return self.solver.apply_inverse_ofield_operator(self.solver.ofield_vector(self.cell_index), self.cell_index)


class CellModelOfieldOperator(MutableStateCellModelOperator):

    def __init__(self, solver, cell_index, dt):
        self.solver = solver
        self.cell_index = cell_index
        self.dt = dt
        self.linear = False
        self.source = BlockVectorSpace([self.solver.ofield_solution_space,
                                        BlockVectorSpace([self.solver.pfield_solution_space,
                                                          self.solver.ofield_solution_space,
                                                          self.solver.stokes_solution_space])])
        self.range = self.solver.ofield_solution_space

    def _set_state(self, U):
        self.solver.set_pfield_vec(0, U._blocks[0]._list[0])
        self.solver.set_ofield_vec(0, U._blocks[1]._list[0])
        self.solver.set_stokes_vec(U._blocks[2]._list[0])
        self.solver.prepare_ofield_operator(self.dt, self.cell_index)
        return CellModelOfieldFixedOperator(self.solver, self.cell_index, self.dt)


class CellModelStokesFixedOperator(OperatorBase):

    def __init__(self, solver):
        self.__auto_init(locals())
        self.linear = True
        self.source = self.range = self.solver.stokes_solution_space

    def apply(self, U, mu=None):
        return self.solver.apply_stokes_operator(U)

    def apply_inverse(self, V, mu=None, least_squares=False):
        assert sum(V.norm()) == 0., "Not implemented for non-zero rhs!"
        assert not least_squares, "Least squares not implemented!"
        return self.solver.apply_inverse_stokes_operator()


class CellModelStokesOperator(MutableStateCellModelOperator):

    def __init__(self, solver):
        self.solver = solver
        self.linear = False
        self.source = BlockVectorSpace([self.solver.stokes_solution_space,
                                        BlockVectorSpace([self.solver.pfield_solution_space,
                                                          self.solver.ofield_solution_space])])
        self.range = self.solver.stokes_solution_space

    def _set_state(self, U):
        self.solver.set_pfield_vec(0, U._blocks[0]._list[0])
        self.solver.set_ofield_vec(0, U._blocks[1]._list[0])
        self.solver.prepare_stokes_operator()
        return CellModelStokesFixedOperator(self.solver)


# class RestrictedCellModelPfieldOperator(RestrictedDuneOperatorBase):

#     linear = False

#     def __init__(self, solver, cell_index, dt, dofs):
#         self.solver = solver
#         self.dofs = dofs
#         dofs_as_list = [int(i) for i in dofs]
#         self.solver.impl.prepare_restricted_pfield_op(dofs_as_list)
#         super(RestrictedCellModelPfieldOperator, self).__init__(solver, self.solver.impl.len_source_dofs(), len(dofs))

#     def apply(self, U, mu=None):
#         assert U in self.source
#         U = DuneXtLaListVectorSpace.from_numpy(U.to_numpy())
#         ret = [
#             DuneXtLaVector(self.solver.impl.apply_restricted_pfield_op(u.impl, self.cell_index, self.dt)).to_numpy(True) for u in U._list
#         ]
#         return self.range.make_array(ret)


def create_and_scatter_cellmodel_parameters(comm,
                                            Re_min=1e-14,
                                            Re_max=1e-4,
                                            Fa_min=0.1,
                                            Fa_max=10,
                                            xi_min=0.1,
                                            xi_max=10):
    ''' Samples all 3 parameters uniformly with the same width and adds random parameter combinations until
        comm.Get_size() parameters are created. After that, parameter combinations are scattered to ranks. '''
    num_samples_per_parameter = int(comm.Get_size()**(1. / 3.) + 0.1)
    sample_width_Re = (Re_max - Re_min) / (num_samples_per_parameter - 1) if num_samples_per_parameter > 1 else 1e10
    sample_width_Fa = (Fa_max - Fa_min) / (num_samples_per_parameter - 1) if num_samples_per_parameter > 1 else 1e10
    sample_width_xi = (xi_max - xi_min) / (num_samples_per_parameter - 1) if num_samples_per_parameter > 1 else 1e10
    Re_range = np.arange(Re_min, Re_max + 1e-15, sample_width_Re)
    Fa_range = np.arange(Fa_min, Fa_max + 1e-15, sample_width_Fa)
    xi_range = np.arange(xi_min, xi_max + 1e-15, sample_width_xi)
    parameters_list = []
    for Re in Re_range:
        for Fa in Fa_range:
            for xi in xi_range:
                parameters_list.append([Re, Fa, xi])
    while len(parameters_list) < comm.Get_size():
        parameters_list.append(
            [random.uniform(Re_min, Re_max),
             random.uniform(Fa_min, Fa_max),
             random.uniform(xi_min, xi_max)])
    return comm.scatter(parameters_list, root=0)


def calculate_cellmodel_trajectory_errors(modes, testcase, t_end, dt, grid_size_x, grid_size_y, mu):
    errs = [0.]*len(modes)
    # modes has length 2*num_cells+1
    nc = (len(modes)-1) // 2
    solver = CellModelSolver(testcase, t_end, grid_size_x, grid_size_y, mu)
    n = 0
    while not solver.finished():
        print("timestep: ", n)
        next_vectors = solver.next_n_timesteps(1, dt)
        for k in range(nc):
            res = next_vectors[k] - modes[k].lincomb(next_vectors[k].dot(solver.apply_pfield_product_operator(modes[k])))
            errs[k] += np.sum(res.pairwise_dot(solver.apply_pfield_product_operator(res)))
            res = next_vectors[nc+k] - modes[nc+k].lincomb(next_vectors[nc+k].dot(solver.apply_ofield_product_operator(modes[nc+k])))
            errs[nc+k] += np.sum(res.pairwise_dot(solver.apply_ofield_product_operator(res)))
        res = next_vectors[2*nc] - modes[2*nc].lincomb(next_vectors[2*nc].dot(solver.apply_stokes_product_operator(modes[2*nc])))
        errs[2*nc] += np.sum(res.pairwise_dot(solver.apply_stokes_product_operator(res)))
        n += 1
    return errs


def calculate_mean_cellmodel_projection_errors(modes,
                                              testcase,
                                              t_end,
                                              dt,
                                              grid_size_x,
                                              grid_size_y,
                                              mu,
                                              mpi_wrapper,
                                              with_half_steps=True):
    trajectory_errs = calculate_cellmodel_trajectory_errors(modes, testcase, t_end, dt, grid_size_x, grid_size_y, mu)
    errs = [0.]*len(modes)
    for index, trajectory_err in enumerate(trajectory_errs):
            trajectory_err = mpi_wrapper.comm_world.gather(trajectory_err, root=0)
            if mpi_wrapper.rank_world == 0:
                errs[index] = np.sqrt(np.sum(trajectory_err))
    return errs


def calculate_cellmodel_errors(modes,
                              testcase,
                              t_end,
                              dt,
                              grid_size_x,
                              grid_size_y,
                              mu,
                              mpi_wrapper,
                              logfile=None):
    ''' Calculates projection error. As we cannot store all snapshots due to memory restrictions, the
        problem is solved again and the error calculated on the fly'''
    start = timer()
    errs = calculate_mean_cellmodel_projection_errors(modes, testcase, t_end, dt, grid_size_x, grid_size_y, mu, mpi_wrapper)
    elapsed = timer() - start
    if mpi_wrapper.rank_world == 0 and logfile is not None:
        logfile.write("Time used for calculating error: " + str(elapsed) + "\n")
        nc = (len(modes) - 1) // 2
        for k in range(nc):
            logfile.write("L2 error for {}-th pfield is: {}\n".format(k, errs[k]))
            logfile.write("L2 error for {}-th ofield is: {}\n".format(k, errs[nc+k]))
        logfile.write("L2 error for stokes is: {}\n".format(errs[2*nc]))
        logfile.close()
    return errs


def get_num_chunks_and_num_timesteps(t_end, dt, chunk_size):
    num_time_steps = math.ceil(t_end / dt) + 1.
    num_chunks = int(math.ceil(num_time_steps / chunk_size))
    last_chunk_size = num_time_steps - chunk_size * (num_chunks - 1)
    assert num_chunks >= 2
    assert 1 <= last_chunk_size <= chunk_size
    return num_chunks, num_time_steps
