from __future__ import print_function

import inspect
import numpy as np
import pymatsolver
import sys
import warnings

import properties
from properties.utils import undefined

from discretize.base import BaseMesh
from discretize import TensorMesh
from discretize.utils import meshTensor

from . import props
from .data import SyntheticData, Data
from .survey import BaseSurvey
from .utils import Counter, timeIt, count, mkvc

__all__ = ['LinearSimulation', 'ExponentialSinusoidSimulation']


##############################################################################
#                                                                            #
#                             Custom Properties                              #
#                                                                            #
##############################################################################

class TimeStepArray(properties.Array):

    class_info = "an array or list of tuples specifying the mesh tensor"

    def validate(self, instance, value):
        if isinstance(value, list):
            value = meshTensor(value)
        return super(TimeStepArray, self).validate(instance, value)


class Class(properties.Property):

    class_info = "a property that is an uninstantiated class"

    def __init__(self, doc, **kwargs):
        default = kwargs.pop('default', None)
        super(Class, self).__init__(doc, **kwargs)
        if default is not None:
            self._parent_module = default.__module__
            print(default)
            print(self._parent_module)
            self.default = default

    @property
    def default(self):
        """Default value of the Property"""
        return getattr(self, '_default', self._class_default)

    @default.setter
    def default(self, value):
        self.validate(None, value)
        self._default = value

    def validate(self, instance, value):
        if inspect.isclass(value) is False:
            extra = (
                "Expected an uninstantiated class. The provided value is not"
            )
            self.error(instance, value, TypeError, extra)
        self._parent_module = value.__module__
        return value

    def serializer(self, value, **kwargs):
        return "{}.{}".format(self._parent_module, value.__name__)

    def deserializer(self, value, **kwargs):
        name = value.split(".")
        try:
            module = sys.modules[".".join(name[:-1])]
        except KeyError:
            raise ImportError(
                "{} not found. Please install {}".format(
                    ".".join(value, name[0])
                )
            )
        return getattr(module, name[-1])

    def sphinx(self):
        """Basic docstring formatted for Sphinx docs"""
        default_val = self.default
        default_str = '{}'.format(self.default)
        try:
            if default_val is None or default_val is undefined:
                default_str = ''
            elif len(default_val) == 0:                                        #pylint: disable=len-as-condition
                default_str = ''
            else:
                default_str = ', Default: {}'.format(default_str)
        except TypeError:
            default_str = ', Default: {}'.format(default_str)

        prop_doc = super(properties.Property, self).sphinx()
        prop_doc = None
        return '{doc}{default}'.format(doc=prop_doc, default=default_str)




##############################################################################
#                                                                            #
#                       Simulation Base Classes                              #
#                                                                            #
##############################################################################

class BaseSimulation(props.HasModel):
    """
    BaseSimulation is the base class for all geophysical forward simulations in
    SimPEG.
    """


    ###########################################################################
    # Properties

    _REGISTRY = {}

    mesh = properties.Instance("a discretize mesh instance", BaseMesh)

    survey = properties.Instance("a survey object", BaseSurvey)

    counter = properties.Instance("A SimPEG.Utils.Counter object", Counter)

    # TODO: need to implement a serializer for this & setter
    solver = Class(
        "Linear algebra solver (e.g. from pymatsolver)",
        # default=pymatsolver.Solver
    )

    solver_opts = properties.Dictionary(
        "solver options as a kwarg dict", default={}
    )

    def _reset(self, name=None):
        """Revert specified property to default value

        If no property is specified, all properties are returned to default.
        """
        if name is None:
            for key in self._props:
                if isinstance(self._props[key], properties.basic.Property):
                    self._reset(key)
            return
        if name not in self._props:
            raise AttributeError("Input name '{}' is not a known "
                                 "property or attribute".format(name))
        if not isinstance(self._props[name], properties.basic.Property):
            raise AttributeError("Cannot reset GettableProperty "
                                 "'{}'".format(name))
        if name in self._defaults:
            val = self._defaults[name]
        else:
            val = self._props[name].default
        # if callable(val):
        #     val = val()
        setattr(self, name, val)

    ###########################################################################
    # Properties and observers

    @properties.observer('mesh')
    def _update_registry(self, change):
        self._REGISTRY.update(change['value']._REGISTRY)

    #: List of strings, e.g. ['_MeSigma', '_MeSigmaI']
    # TODO: rename to _delete_on_model_update
    deleteTheseOnModelUpdate = []

    #: List of matrix names to have their factors cleared on a model update
    clean_on_model_update = []

    @properties.observer('model')
    def _on_model_update(self, change):
        if change['previous'] is change['value']:
            return
        if (
            isinstance(change['previous'], np.ndarray) and
            isinstance(change['value'], np.ndarray) and
            np.allclose(change['previous'], change['value'])
        ):
            return

        # cached properties to delete
        for prop in self.deleteTheseOnModelUpdate:
            if hasattr(self, prop):
                delattr(self, prop)

        # matrix factors to clear
        for mat in self.clean_on_model_update:
            if getattr(self, mat, None) is not None:
                getattr(self, mat).clean()  # clean factors
                setattr(self, mat, None)  # set to none

    @property
    def Solver(self):
        """
        Deprecated solver property. Please use :code:`simulation.solver`
        instead
        """
        warnings.warn(
            "simulation.Solver will be deprecaited and replaced with "
            "simulation.solver. Please update your code accordingly",
            DeprecationWarning
        )
        return self.solver

    @Solver.setter
    def Solver(self, value):
        warnings.warn(
            "simulation.Solver will be deprecaited and replaced with "
            "simulation.solver. Please update your code accordingly",
            DeprecationWarning
        )
        self.solver = value

    @property
    def solverOpts(self):
        """
        Deprecated solver options. Please use :code:`simulation.solver_opts`
        instead
        """
        warnings.warn(
            "simulation.solverOpts will be deprecaited and replaced with "
            "simulation.solver_opts. Please update your code accordingly",
            DeprecationWarning
        )
        return self.solver

    @solverOpts.setter
    def solverOpts(self, value):
        warnings.warn(
            "simulation.solverOpts will be deprecaited and replaced with "
            "simulation.solver_opts. Please update your code accordingly",
            DeprecationWarning
        )
        self.solver_opts = value

    ###########################################################################
    # Instantiation

    def __init__(self, mesh=None, **kwargs):
        # raise exception if user tries to set "mapping"
        if 'mapping' in kwargs.keys():
            raise Exception(
                'Depreciated (in 0.4.0): use one of {}'.format(
                    [p for p in self._props.keys() if 'Map' in p]
                )
            )

        super(BaseSimulation, self).__init__(mesh=mesh, **kwargs)

        if 'solver' not in kwargs.keys() and 'Solver' not in kwargs.keys():
            self.solver = pymatsolver.Solver



    ###########################################################################
    # Methods

    def pair(self, survey):
        """
        Deprecated pairing method. Please use :code:`simulation.survey=survey`
        instead
        """
        warnings.warn(
            "simulation.pair(survey) will be depreciated. Please use "
            "simulation.survey = survey",
            DeprecationWarning
        )
        self.survey = survey

    def fields(self, m=None):
        """
        u = fields(m)
        The field given the model.
        :param numpy.array m: model
        :rtype: numpy.array
        :return: u, the fields
        """
        raise NotImplementedError(
            "fields has not been implemented for this "
        )

    def dpred(self, m=None, f=None):
        """
        dpred(m, f=None)
        Create the projected data from a model.
        The fields, f, (if provided) will be used for the predicted data
        instead of recalculating the fields (which may be expensive!).
        .. math::
            d_\\text{pred} = P(f(m))
        Where P is a projection of the fields onto the data space.
        """
        if self.survey is None:
            raise AttributeError(
                "The survey has not yet been set and is required to compute "
                "data. Please set the survey for the simulation: "
                "simulation.survey = survey"
            )

        if f is None:
            if m is None:
                m = self.model
            f = self.fields(m)

        data = Data(self.survey)
        for src in self.survey.source_list:
            for rx in src.receiver_list:
                data[src, rx] = rx.eval(src, self.mesh, f)
        return mkvc(data)

    @timeIt
    def Jvec(self, m, v, f=None):
        """
        Jv = Jvec(m, v, f=None)
        Effect of J(m) on a vector v.
        :param numpy.array m: model
        :param numpy.array v: vector to multiply
        :param Fields f: fields
        :rtype: numpy.array
        :return: Jv
        """
        raise NotImplementedError('Jvec is not yet implemented.')

    @timeIt
    def Jtvec(self, m, v, f=None):
        """
        Jtv = Jtvec(m, v, f=None)
        Effect of transpose of J(m) on a vector v.
        :param numpy.array m: model
        :param numpy.array v: vector to multiply
        :param Fields f: fields
        :rtype: numpy.array
        :return: JTv
        """
        raise NotImplementedError('Jt is not yet implemented.')

    @timeIt
    def Jvec_approx(self, m, v, f=None):
        """Jvec_approx(m, v, f=None)
        Approximate effect of J(m) on a vector v
        :param numpy.array m: model
        :param numpy.array v: vector to multiply
        :param Fields f: fields
        :rtype: numpy.array
        :return: approxJv
        """
        return self.Jvec(m, v, f)

    @timeIt
    def Jtvec_approx(self, m, v, f=None):
        """Jtvec_approx(m, v, f=None)
        Approximate effect of transpose of J(m) on a vector v.
        :param numpy.array m: model
        :param numpy.array v: vector to multiply
        :param Fields f: fields
        :rtype: numpy.array
        :return: JTv
        """
        return self.Jtvec(m, v, f)

    @count
    def residual(self, m, dobs, f=None):
        """residual(m, dobs, f=None)
            :param numpy.array m: geophysical model
            :param numpy.array f: fields
            :rtype: numpy.array
            :return: data residual
            The data residual:
            .. math::
                \mu_\\text{data} = \mathbf{d}_\\text{pred} - \mathbf{d}_\\text{obs}
        """
        return mkvc(self.dpred(m, f=f) - dobs)

    def make_synthetic_data(
        self, m, standard_deviation=0.05, f=None, add_noise=False, **kwargs
    ):
        """
        Make synthetic data given a model, and a standard deviation.
        :param numpy.array m: geophysical model
        :param numpy.array standard_deviation: standard deviation
        :param numpy.array f: fields for the given model (if pre-calculated)
        """

        std =  kwargs.pop('std', None)
        if std is not None:
            standard_deviation = std

        dclean = self.dpred(m, f=f)
        if add_noise is True:
            noise = standard_deviation*abs(dclean)*np.random.randn(*dclean.shape)
            dobs = dclean + noise
        else:
            dobs = dclean

        return SyntheticData(
            survey=self.survey, dobs=dobs, dclean=dclean,
            standard_deviation=standard_deviation,
        )

    def makeSyntheticData(self, m, standard_deviation=0.05, f=None):
        warnings.warn(
            "makeSyntheticData will be depreciated in favor of "
            "make_synthetic_data. Please update your code to use "
            "make_synthetic_data", DeprecationWarning
        )
        return self.make_synthetic_data(
            m, standard_deviation=standard_deviation, f=f
        )


class BaseTimeSimulation(BaseSimulation):
    """
    Base class for a time domain simulation
    """

    time_steps = TimeStepArray(
        """
        Sets/gets the time steps for the time domain simulation.
        You can set as an array of dt's or as a list of tuples/floats.
        Tuples must be length two with [..., (dt, repeat), ...]
        For example, the following setters are the same::
            sim.time_steps = [(1e-6, 3), 1e-5, (1e-4, 2)]
            sim.time_steps = np.r_[1e-6,1e-6,1e-6,1e-5,1e-4,1e-4]
        """,
        dtype=float
    )

    t0 = properties.Float(
        "Origin of the time discretization",
        default=0.0
    )

    def __init__(self, mesh=None, **kwargs):
        super(BaseTimeSimulation, self).__init__(mesh=mesh, **kwargs)

    @properties.observer('time_steps')
    def _remove_time_mesh_on_time_step_update(self, change):
        del self.time_mesh

    @properties.observer('t0')
    def _remove_time_mesh_on_t0_update(self, change):
        del self.time_mesh

    @property
    def time_mesh(self):
        if getattr(self, '_time_mesh', None) is None:
            self._time_mesh = TensorMesh([self.time_steps], x0=[self.t0])
        return self._time_mesh

    @time_mesh.deleter
    def time_mesh(self):
        if hasattr(self, '_time_mesh'):
            del self._time_mesh

    @property
    def nT(self):
        return self.time_mesh.nC

    @property
    def times(self):
        "Modeling times"
        return self.time_mesh.vectorNx

    @property
    def timeSteps(self):
        warnings.warn(
            "timeSteps will be depreciated in favor of time_steps. "
            "Please update your code accordingly"
        )
        return self.time_steps

    @timeSteps.setter
    def timeSteps(self, value):
        warnings.warn(
            "timeSteps will be depreciated in favor of time_steps. "
            "Please update your code accordingly"
        )
        self.time_steps = value

    @property
    def timeMesh(self):
        warnings.warn(
            "timeMesh will be depreciated in favor of time_mesh. "
            "Please update your code accordingly"
        )
        return self.time_mesh

    def dpred(self, m=None, f=None):
        """
        dpred(m, f=None)
        Create the projected data from a model.
        The fields, f, (if provided) will be used for the predicted data
        instead of recalculating the fields (which may be expensive!).
        .. math::
            d_\\text{pred} = P(f(m))
        Where P is a projection of the fields onto the data space.
        """
        if self.survey is None:
            raise AttributeError(
                "The survey has not yet been set and is required to compute "
                "data. Please set the survey for the simulation: "
                "simulation.survey = survey"
            )

        if f is None:
            f = self.fields(m)

        data = Data(self.survey)
        for src in self.survey.source_list:
            for rx in src.receiver_list:
                data[src, rx] = rx.eval(src, self.mesh, self.time_mesh, f)
        return data.dobs


##############################################################################
#                                                                            #
#                           Linear Simulation                                #
#                                                                            #
##############################################################################

class LinearSimulation(BaseSimulation):
    """
    Class for a linear simulation of the form
    .. math::
        d = Gm
    where :math:`d` is a vector of the data, `G` is the simulation matrix and
    :math:`m` is the model.
    Inherit this class to build a linear simulatio.
    """

    linear_model, model_map, model_deriv = props.Invertible(
        "The model for a linear problem"
    )

    mesh = properties.Instance(
        "a discretize mesh instance",
        BaseMesh,
        required=True
    )

    def __init__(self, mesh=None, **kwargs):
        super(LinearSimulation, self).__init__(mesh=mesh, **kwargs)
        self.survey = BaseSurvey()

        # set the number of data
        if getattr(self, 'G', None) is not None:
            self.survey._vnD = np.r_[self.G.shape[0]]

    @property
    def G(self):
        warnings.warn("G has not been implemented for the simulation")
        return None

    def fields(self, m):
        self.model = m
        return self.G.dot(self.model)

    def dpred(self, m=None, f=None):
        if m is not None:
            self.model = m
        if f is not None:
            return f
        return self.fields(self.model)

    def getJ(self, m, f=None):
        self.model = m
        return self.G.dot(self.model_deriv)

    def Jvec(self, m, v, f=None):
        self.model = m
        return self.G.dot(self.model_deriv * v)

    def Jtvec(self, m, v, f=None):
        self.model = m
        return self.model_deriv.T * self.G.T.dot(v)


class ExponentialSinusoidSimulation(LinearSimulation):
    """
    This is the simulation class for the linear problem consisting of
    exponentially decaying sinusoids. The rows of the G matrix are

    .. math::

        \\int_x e^{p j_k x} \\cos(\\pi q j_k x) \\quad, j_k \\in [j_0, ..., j_n]
    """
    n_kernels = properties.Integer(
        "number of kernels defining the linear problem",
        default = 20
    )

    p = properties.Float(
        "rate of exponential decay of the kernel",
        default=-0.25
    )

    q = properties.Float(
        "rate of oscillation of the kernel",
        default = 0.25
    )

    j0 = properties.Float(
        "maximum value for :math:`j_k = j_0`",
        default = 0.
    )

    jn = properties.Float(
        "maximum value for :math:`j_k = j_n`",
        default = 60.
    )

    def __init__(self, **kwargs):
        super(ExponentialSinusoidSimulation, self).__init__(**kwargs)

    @property
    def jk(self):
        """
        Parameters controlling the spread of kernel functions
        """
        if getattr(self, '_jk', None) is None:
            self._jk = np.linspace(self.j0, self.jn, self.n_kernels)
        return self._jk

    def g(self, k):
        """
        Kernel functions for the decaying oscillating exponential functions.
        """
        return (
            np.exp(self.p*self.jk[k]*self.mesh.vectorCCx) *
            np.cos(np.pi*self.q*self.jk[k]*self.mesh.vectorCCx)
        )

    @property
    def G(self):
        """
        Matrix whose rows are the kernel functions
        """
        if getattr(self, '_G', None) is None:
            G = np.empty((self.n_kernels, self.mesh.nC))

            for i in range(self.n_kernels):
                G[i, :] = self.g(i) * self.mesh.hx

            self._G = G
        return self._G