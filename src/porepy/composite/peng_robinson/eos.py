"""This module contains a class implementing the Peng-Robinson EoS for either
a liquid- or gas-like phase."""
from __future__ import annotations

import logging
import numbers
from dataclasses import dataclass
from typing import Callable, Literal

import numpy as np
import scipy.sparse as sps

import porepy as pp
from porepy.numerics.ad.operator_functions import NumericType

from .._core import ENERGY_SCALE, PRESSURE_SCALE, R_IDEAL, safe_sum
from ..phase import AbstractEoS, PhaseProperties
from .mixing import VanDerWaals
from .pr_bip import load_bip
from .pr_components import Component_PR

__all__ = ["PhaseProperties_cubic", "PengRobinsonEoS"]

logger = logging.getLogger(__name__)


def root_smoother(
    Z_L: pp.ad.AdArray, Z_I: pp.ad.AdArray, Z_G: pp.ad.AdArray, s: float
) -> tuple[pp.ad.AdArray, pp.ad.AdArray]:
    """Smoothing procedure on boundaries of three-root-region.

    Smooths the value and Jacobian rows of the liquid and gas root close to
    phase boundaries.

    See Also:
        `Vu et al. (2021), Section 6.
        <https://doi.org/10.1016/j.matcom.2021.07.015>`_

    Parameters:
        Z_L: Liquid-like root.
        Z_I: Intermediate root.
        Z_G: Gas-like root.
        s: smoothing factor.

    Returns:
        A tuple containing the smoothed liquid and gas root as AD arrays

    """
    # proximity:
    # If close to 1, intermediate root is close to gas root.
    # If close to 0, intermediate root is close to liquid root.
    # values bound by [0,1]
    proximity = (Z_I - Z_L) / (Z_G - Z_L)
    if isinstance(proximity, pp.ad.AdArray):
        proximity = proximity.val

    # average intermediate and gas root for gas root smoothing
    W_G = (Z_I + Z_G) / 2
    # analogously for liquid root
    W_L = (Z_I + Z_L) / 2

    v_G = _gas_smoother(proximity, s)
    v_L = _liquid_smoother(proximity, s)

    return (
        Z_L * (1 - v_L) + W_L * v_L,  # weights are arrays, Z possibly AD -> * right
        Z_G * (1 - v_G) + W_G * v_G,
    )


def _gas_smoother(proximity: np.ndarray, s: float) -> np.ndarray:
    """Smoothing function for three-root-region where the intermediate root comes
    close to the gas root.

    Parameters:
        proximity: An array containing the proximity between the intermediate
            root and the liquid root, relative to the difference in liquid and
            gas root
        s: smoothing factor

    Returns:
        The smoothing weight for the gas root.

    """
    # smoother starts with zero values
    smoother = np.zeros(proximity.shape[0])
    # values around smoothing parameter are constructed according to Vu
    upper_bound = proximity < 1 - s
    lower_bound = (1 - 2 * s) < proximity
    bound = np.logical_and(upper_bound, lower_bound)

    bound_smoother = (proximity[bound] - (1 - 2 * s)) / s
    bound_smoother = bound_smoother**2 * (3 - 2 * bound_smoother)

    smoother[bound] = bound_smoother
    # where proximity is close to one, set value of one
    smoother[proximity >= 1 - s] = 1.0

    return smoother


def _liquid_smoother(proximity: np.ndarray, s: float) -> np.ndarray:
    """Smoothing function for three-root-region where the intermediate root comes
    close to the liquid root.

    Parameters:
        proximity: An array containing the proximity between the intermediate
            root and the liquid root, relative to the difference in liquid and
            gas root
        s: smoothing factor

    Returns:
        The smoothing weight for the liquid root

    """
    # smoother starts with zero values
    smoother = np.zeros(proximity.shape[0])
    # values around smoothing parameter are constructed according to Vu
    upper_bound = proximity < 2 * s
    lower_bound = s < proximity
    bound = np.logical_and(upper_bound, lower_bound)

    bound_smoother = (proximity[bound] - s) / s
    bound_smoother = (-1) * bound_smoother**2 * (3 - 2 * bound_smoother) + 1

    smoother[bound] = bound_smoother
    # where proximity is close to zero, set value of one
    smoother[proximity <= s] = 1.0

    return smoother


@dataclass(frozen=True)
class PhaseProperties_cubic(PhaseProperties):
    """Extended phase properties resulting from computations using a cubic EoS.

    The properties here are not necessarily required by the general framework.

    """

    a: NumericType
    """Cohesion term ``a`` in the cubic EoS."""

    dT_a: NumericType
    """The derivative of the cohesion w.r.t. the temperature."""

    b: NumericType
    """Covolume term ``b`` in the cubic EoS."""

    A: NumericType
    """Non-dimensional cohesion (see :attr:`a`)."""

    B: NumericType
    """Non-dimensional covolume (see :attr:`b`)"""

    Z: NumericType
    """Compressibility factor."""


class PengRobinsonEoS(AbstractEoS):
    """A class implementing thermodynamic properties resulting from the Peng-Robinson
    equation of state

        ``p = R * T / (v - b) - a / (b**2 + 2 * v * b - b**2)``.

    Note:
        1. The various methods providing thermodynamic quantities are AD-compatible.
           They can be wrapped into AD-Functions and are able to take Ad-Arrays as
           input.
        2. Calling any thermodynamic property with molar fractions ``X`` as arguments
           raises errors if the number of passed fractions does not match the
           number of modelled components in :attr:`components`.

           The order of fractions arguments must correspond to the order in
           :attr:`components`.
        3. As of now, supercritical phases are not really supported, just indicated.
           Be aware of the limitations of the Peng-Robinson model!

    For a list of computed properties, see dataclass :class:`PhaseProperties_cubic`.

    References:
        [1]: `Peng, Robinson (1976) <https://doi.org/10.1021/i160057a011>`_
        [2]: `Ben Gharbia et al. (2021) <https://doi.org/10.1051/m2an/2021075>`_
        [3]: `Vu et al. (2021) <https://doi.org/10.1016/j.matcom.2021.07.015>`_
        [4]: `Zhu, Okuno (2014) <http://dx.doi.org/10.1016/j.fluid.2014.07.003>`_
        [5]: `Zhu, Okuno (2015) <https://onepetro.org/spersc/proceedings/15RSS/
             1-15RSS/D011S001R002/183434>`_
        [6]: `Connolly et al. (2021) <https://doi.org/10.1016/j.ces.2020.116150>`_

    Parameters:
        gaslike: A bool indicating if if the gas-like root of the cubic polynomial
            should be used for computations.

            If False, the liquid-like root is used.
        mixingrule: ``default='VdW'``

            Name of the mixing rule to be applied.
        transition_smoothing: ``default=1e-4``

            A small number to determine proximity between 3-root and double-root case.

            See also `Vu et al. (2021), Section 6. <https://doi.org/10.1016/
            j.matcom.2021.07.015>`_ .
        eps: ``default=1e-16``

            A small number defining the numerical zero.

            Used for the computation of roots to distinguish between phase-regions.
        *args: Placeholder in case of inheritance.
        **kwargs: Placeholder in case of inheritance.

    """

    A_CRIT: float = (
        1
        / 512
        * (
            -59
            + 3 * np.cbrt(276231 - 192512 * np.sqrt(2))
            + 3 * np.cbrt(276231 + 192512 * np.sqrt(2))
        )
    )
    """Critical, non-dimensional cohesion value in the Peng-Robinson EoS,
    ~ 0.457235529."""

    B_CRIT: float = (
        1
        / 32
        * (-1 - 3 * np.cbrt(16 * np.sqrt(2) - 13) + 3 * np.cbrt(16 * np.sqrt(2) + 13))
    )
    """Critical, non-dimensional covolume in the Peng-Robinson EoS, ~ 0.077796073."""

    Z_CRIT: float = (
        1 / 32 * (11 + np.cbrt(16 * np.sqrt(2) - 13) - np.cbrt(16 * np.sqrt(2) + 13))
    )
    """Critical compressibility factor in the Peng-Robinson EoS, ~ 0.307401308."""

    def __init__(
        self,
        gaslike: bool,
        *args,
        mixingrule: Literal["VdW"] = "VdW",
        smoothing_factor: float = 1e-4,
        eps: float = 1e-14,
        **kwargs,
    ) -> None:
        super().__init__(gaslike)

        self._a_crit_vals: tuple[float] = []
        """Critical cohesion values per component, using EoS specific critical value.

        Computed in setter for :meth:`components`.

        """

        self._b_crit_vals: tuple[float] = []
        """Critical covolume values per component, using EoS-specific critical value.

        Computed in setter for :meth:`components`.

        """

        self._a_cor_vals: tuple[float] = []
        """Cohesion correction weights, appearing in the linearized alpha-correction of
        the cohesion.

        Computed in setter for :meth:`components`.

        """

        self._bip_vals: np.ndarray = np.array([])
        """A matrix  in strictly upper-triangle form containing the constant
        binary interaction parameters between components at indices ``[i][j]``.

        The values are loaded once during the setter for :meth:`components`.

        """

        self._custom_bips: dict[
            str, Callable[[NumericType], tuple[NumericType, NumericType]]
        ] = dict()
        """A map between indices for :attr:`_bip_vals` (upper triangle) and
        custom implementation of BIPs (temperature-dependent), if available.

        See :attr:`~porepy.composite.peng_robinson.pr_components.Component_PR.bip_map`.

        """

        self.gaslike: bool = bool(gaslike)
        """Flag passed at instantiation denoting the state of matter, liquid or gas."""

        assert mixingrule in ["VdW"], f"Unknown mixing rule {mixingrule}."
        self.mixingrule: str = mixingrule
        """The mixing rule passed at instantiation."""

        self.eps: float = eps
        """Passed at instantiation."""

        self.smoothing_factor: float = smoothing_factor
        """Passed at instantiation."""

    def _num_frac_check(self, X) -> None:
        """Auxiliary method to check the number of passed fractions.
        Raises an error if number does not match the number of present components."""
        if len(X) != len(self.components):
            raise ValueError(
                f"{len(X)} fractions given but "
                + f"{len(self.components)} components present."
            )

    @property
    def components(self) -> list[Component_PR]:
        """The child class setter calculates EoS specific values per set component.

        Values like critical cohesion and covolume values, corrective parameters and
        binary interaction parameters are obtained only once and stored during in the
        setter method.

        The setter additionally checks for the availability of BIPs and throws
        respective warnings. Unavailable BIPs are set to zero.

        Todo:
            Should it be a warning or an error?

        Warning:
            If two components ``i`` and ``j``, with ``i < j``, have both BIPs
            BIPs implemented for each other in
            :attr:`~porepy.composite.peng_robinson.pr_components.Component_PR.bip_map`,
            the first implementation of ``i`` is taken and a warning is emitted.

        Parameters:
            components: A list of Peng-Robinson compatible components.

        Raises:
            RuntimeError: If two different components implement models for BIP for each
                other (it is impossible to decide which one to use).

        """
        return AbstractEoS.components.fget(self)

    @components.setter
    def components(self, components: list[Component_PR]) -> None:

        a_crits: list[float] = list()
        b_crits: list[float] = list()
        kappa_cors: list[float] = list()

        nc = len(components)

        # computing constant parameters
        for comp in components:
            a_crits.append(self._a_crit(comp.p_crit, comp.T_crit))
            b_crits.append(self._b_crit(comp.p_crit, comp.T_crit))
            kappa_cors.append(self._kappa_cor(comp.omega))

        self._b_crit_vals = tuple(b_crits)
        self._a_crit_vals = tuple(a_crits)
        self._kappa_vals = tuple(kappa_cors)

        # prepare storage for constant bips
        self._bip_vals = np.zeros((nc, nc))

        # storing missing bips and custom bip callables
        bip_callables: dict[
            str, Callable[[NumericType], tuple[NumericType, NumericType]]
        ] = dict()

        for i in range(nc):
            comp_i = components[i]
            # storage for custom BIPs with other component, if custom available.
            customs_for_i = []
            # check if component i has custom BIPs implemented
            if hasattr(comp_i, "bip_map"):
                for j in range(i + 1, nc):
                    comp_j = components[j]
                    bip_c = comp_i.bip_map.get(comp_j.CASr_number, None)
                    # if found, store and mark to check it with the other component
                    if bip_c is not None:
                        customs_for_i.append(j)
                        bip_callables.update({(i, j): bip_c})

            for j in range(nc):  # strictly upper triangle matrix
                comp_j = components[j]

                # use custom models if implemented
                if hasattr(comp_j, "bip_map"):
                    bip_c = comp_j.bip_map.get(comp_i.CASr_number, None)
                    # check if component i has already implemented a bip
                    # If not, check if j has it implemented, store and continue
                    # if it has, warn if double implementation detected and continue
                    if j in customs_for_i:
                        if bip_c is not None:
                            logger.warn(
                                "Detected double-implementation of BIP for components"
                                + f"{comp_i.name} and {comp_j.name}."
                                + "\nA fix to the model components if recommended."
                            )
                        continue
                    else:
                        # store callables, if implemented, and continue
                        if bip_c is not None:
                            assert (i, j) not in bip_callables.keys()
                            bip_callables.update({(i, j): bip_c})
                            continue
                        # at this point, the code will continue and try load a database
                        # bip.
                        # This happens if i and j have custom BIPs, but not for each
                        # other

                # try to load BIPs from database, if custom has not been found so far
                bip = load_bip(comp_i.CASr_number, comp_j.CASr_number)
                # TODO: This needs some more thought. How to handle missing BIPs?
                # if bip == 0.0:
                #     logger.warn(
                #         "Loaded a BIP with zero value for"
                #         + f" components {comp_i.name} and {comp_j.name}."
                #     )
                # assert that no custom BIP is overwritten
                assert (i, j) not in bip_callables.keys(), "Overwriting custom BIP."
                self._bip_vals[i, j] = bip

        # If custom implementations for bips were found, store them.
        # Otherwise store an empty dict.
        if bip_callables:
            self._custom_bips = bip_callables
        else:
            self._custom_bips = dict()

        AbstractEoS.components.fset(self, components)

    def compute(
        self,
        p: NumericType,
        T: NumericType,
        X: list[NumericType],
        apply_smoother: bool = False,
        **kwargs,
    ) -> PhaseProperties_cubic:
        """Computes all thermodynamic properties based on the passed state.

        Warning:
            ``p``, ``T``, ``X`` have a union type, meaning the results will be of
            the same. When mixing numpy arrays, porepy's Ad arrays and numbers,
            the user must make sure there will be no compatibility issues.

            This method is not supposed to be used with AD Operator instances.

        Parameters:
            p: Pressure
            T: Temperature
            X: ``len=num_components``

                Fraction per component to be used in the computation,
                ordered as in :meth:`components`.
            apply_smoother: ``default=False``

                If True, a smoothing procedure is applied in the three-root-region,
                where the intermediate root approaches one of the other roots
                (see [3]).

                This is to be used **within** iterative procedures for numerical
                reasons. Once convergence is reached, the true roots should be computed
                without smoothing.
            **kwargs: Placeholder in case of inheritance.

        Raises:
            ValueError: If a mismatch between number passed fractions and modelled
                components is detected.

        Returns:
            A dataclass containing thermodynamic properties resulting from a cubic EoS.

        """
        # sanity check
        self._num_frac_check(X)
        # binary interaction parameters
        bip, dT_bip = self._compute_bips(T)
        # cohesion and covolume, and derivative
        a, dT_a, alpha_a_comps, _ = self._compute_cohesion_terms(T, X, bip, dT_bip)
        b = self._compute_mixture_covolume(X)
        # compute non-dimensional quantities
        A = self._A(a, p, T)
        B = self._B(b, p, T)
        # root
        Z, Z_other = self._Z(A, B, apply_smoother)
        # density
        rho = self._rho(p, T, Z)
        rho_mass = self.get_rho_mass(rho, X)
        # volume
        v = self._v(p, T, Z)
        # departure enthalpy
        h_dep = self._h_dep(T, Z, a, dT_a, b, B)
        h_ideal = self.get_h_ideal(p, T, X)
        h = h_ideal + h_dep

        # Fugacity extensions as per Ben Gharbia 2021
        dxi_a = list()
        if False: #np.any(self.is_extended):
            extend_phi: bool = True
            rho_ext = self._rho(p, T, Z_other)
            G = self._Z_polynom(Z, A, B)
            Gamma_ext = Z * G / ((Z - B) * (Z**2 + 2 * Z * B - B**2))
            dxi_Z_other = list()
            for i in range(len(self.components)):
                dxi_a_ = self._dXi_a(X, alpha_a_comps, bip, i)
                dxi_a.append(dxi_a_)
                dxi_Z_other_ = self._dxi_Z(T, rho_ext, a, b, self._b_crit_vals[i], dxi_a[i])
                dxi_Z_other.append(dxi_Z_other_)
            dZ_other = safe_sum([x * dz for x, dz in zip(X, dxi_Z_other)])
        else:
            extend_phi: bool = False
            for i in range(len(self.components)):
                dxi_a_ = self._dXi_a(X, alpha_a_comps, bip, i)
                dxi_a.append(dxi_a_)
        # fugacity per present component
        phis: list[NumericType] = list()
        for i in range(len(self.components)):
            b_i = self._b_crit_vals[i]
            B_i = self._B(b_i, p, T)
            A_i = dxi_a[i] # * p / (T * R_IDEAL) ** 2
            phi_i = self._phi_i(Z, A_i, A, B_i, B, a)

            if extend_phi:
                w1 = (B - B_i + dZ_other - dxi_Z_other[i]) / Z / 2
                w2 = (B - B_i) / B
                ext_i = Gamma_ext * (w1 + w2)

                ext_i = ext_i[self.is_extended]
                phi_i[self.is_extended] = phi_i[self.is_extended] + ext_i

            phis.append(phi_i)

        # these two are open TODO
        kappa = self._kappa(p, T, Z)
        mu = self._mu(p, T, Z)

        return PhaseProperties_cubic(
            a=a,
            dT_a=dT_a,
            b=b,
            A=A,
            B=B,
            Z=Z,
            rho=rho,
            rho_mass=rho_mass,
            v=v,
            h_ideal=h_ideal,
            h_dep=h_dep,
            h=h,
            phis=phis,
            kappa=kappa,
            mu=mu,
        )

    def _compute_bips(
        self, T: NumericType
    ) -> tuple[list[list[NumericType]], list[list[NumericType]]]:
        """
        Parameters:
            T: Temperature.

        Returns:
            Two nested lists in upper-triangle form containing the binary interaction
            parameters. The first structure contains the parameters, the second
            their temperature-derivatives.

            The lists are such that indexing by ``[i][j]`` gives the interaction
            parameter between components ``i`` and ``j``.

            The lower triangle and the main diagonal of this matrix-like structure
            are filled with zeros, since the interaction parameters are symmetrical.

        """
        # construct first output from the constant bips and their trivial derivative
        bips: list[list[NumericType]] = self._bip_vals.tolist()  # type: ignore
        dT_bips: list[list[NumericType]] = np.zeros(
            self._bip_vals.shape
        ).tolist()  # type: ignore

        # if any custom bips are to be used, update the respective entries
        if self._custom_bips:
            for (
                idx,
                bip_c,
            ) in self._custom_bips.items():
                i, j = idx
                bip, dT_bip = bip_c(T)

                bips[i][j] = bip
                dT_bips[i][j] = dT_bip

        return bips, dT_bips

    def _compute_component_cohesions(
        self, T: NumericType
    ) -> tuple[list[NumericType], list[NumericType]]:
        """
        Parameters:
            T: Temperature

        Returns:
            A 2-tuple containing

            1. temperature-dependent cohesion values per component.
            2. the temperature-derivative of the the cohesion per component.

        """
        a: list[NumericType] = []
        dT_a: list[NumericType] = []

        for i, comp in enumerate(self.components):
            kappa_i = self._kappa_vals[i]
            a_crit_i = self._a_crit_vals[i]
            T_r_i = T / comp.T_crit

            # check if special model for alpha was implemented.
            if hasattr(comp, "alpha"):
                alpha_i = comp.alpha(T)
            else:
                alpha_i = self._a_alpha(kappa_i, T_r_i)

            a_i = a_crit_i * pp.ad.power(alpha_i, 2)
            # outer derivative
            dT_a_i = 2 * a_crit_i * alpha_i
            # inner derivative
            dT_a_i *= (-kappa_i / (2 * comp.T_crit)) * pp.ad.power(T_r_i, -1 / 2)

            a.append(a_i)
            dT_a.append(dT_a_i)

        return a, dT_a

    def _compute_cohesion_terms(
        self,
        T: NumericType,
        X: list[NumericType],
        bip: list[list[NumericType]],
        dT_bip: list[list[NumericType]],
    ) -> tuple[NumericType, NumericType, NumericType, NumericType]:
        """
        Parameters:
            T: Temperature.
            X: ``len=num_components``

                Fraction per component to be used in the computation,
                ordered as in :attr:`components`.
            bip: A nested list or matrix-like structure, such that ``bip[i][j]`` is the
                binary interaction parameter between components ``i`` and ``j``,
                where the indices run over the enumeration of ``X`` and ``a``.

                The matrix-like structure can be an upper diagonal matrix, since the
                interaction parameters are symmetric.
            dT_bip: Same as ``bip``, holding only the temperature-derivative of the
                binary interaction parameters.

        Returns:
            A 4-tuple containing

            - The cohesion ``a`` of the mixture for given thermodynamic state,
              using the assigned mixing rule,
            - its temperature-derivative,
            - cohesion values per component,
            - their temperature, derivatives

        """
        a_c, dT_a_c = self._compute_component_cohesions(T)
        if self.mixingrule == "VdW":
            a_mix, dT_a_mix = VanDerWaals.cohesion(X, a_c, dT_a_c, bip, dT_bip)
            return a_mix, dT_a_mix, a_c, dT_a_c
        else:
            raise ValueError(f"Unknown mixing rule {self.mixingrule}.")

    def _compute_mixture_covolume(self, X: list[NumericType]) -> NumericType:
        """
        Parameters:
            X: ``len=num_components``

                Fraction per component to be used in the computation,
                ordered as in :attr:`components`.

        Returns:
            The covolume ``b`` for given thermodynamic state, using the assigned
            mixing rule.

        """
        if self.mixingrule == "VdW":
            return VanDerWaals.covolume(X, self._b_crit_vals)
        else:
            raise ValueError(f"Unknown mixing rule {self.mixingrule}.")

    # formulae -------------------------------------------------------------------------

    @classmethod
    def _b_crit(cls, p_crit: float, T_crit: float) -> float:
        """
        .. math::

            a_{crit} = A_{crit} * \\frac{R^2 T_{crit}^2}{p_{crit}}

        Parameters:
            p_crit: Critical pressure of a component.
            T_crit: Critical temperature of a component.

        Returns:
            The component-specific critical covolume.

        """
        return cls.B_CRIT * ((R_IDEAL / 1000.0) * T_crit) / p_crit

    @classmethod
    def _a_crit(cls, p_crit: float, T_crit: float) -> float:
        """
        .. math::

            a_{crit} = A_{crit} * \\frac{R T_{crit}}{p_{crit}}

        Parameters:
            p_crit: Critical pressure of a component.
            T_crit: Critical temperature of a component.

        Returns:
            The component-specific critical cohesion.

        """
        return cls.A_CRIT * (R_IDEAL**2 * T_crit**2) / p_crit

    @staticmethod
    def _kappa_cor(omega: float) -> float:
        """
        References:
            `Zhu et al. (2014), Appendix A
            <https://doi.org/10.1016/j.fluid.2014.07.003>`_

        Parameters:
            omega: Acentric factor for a component.

        Returns:
            Returns the cohesion correction parameter depending on a component's
            acentric factor.

        """

        # https://thermo.readthedocs.io/thermo.eos_mix.html#peng-robinson-1978
        if omega < 0.491:
            return 0.37464 + 1.54226 * omega - 0.26992 * omega**2
        else:
            return (
                0.379642
                + 1.48503 * omega
                - 0.164423 * omega**2
                + 0.016666 * omega**3
            )

    @staticmethod
    def _a_alpha(kappa_cor: float, T_r: NumericType) -> NumericType:
        """
        Parameters:
            kappa_cor: Acentric factor-dependent weight in the linearized correction of
                the cohesion for a component.
            T_r: Reduced temperature for a component (divided by the component's
                critical temperature.).

        Returns:
            The root of the linearized correction for the cohesion term.

        """
        return 1 + kappa_cor * (1 - pp.ad.sqrt(T_r))

    def _dXi_a(
        self,
        X: list[NumericType],
        a: list[NumericType],
        bip: list[list[NumericType]],
        i: int,
    ) -> NumericType:
        """Auxiliary method to compute parts of the fugacity coefficients."""
        if self.mixingrule == "VdW":
            return VanDerWaals.dXi_cohesion(X, a, bip, i)
        else:
            raise ValueError(f"Unknown mixing rule {self.mixingrule}.")

    @staticmethod
    def _A(a: NumericType, p: NumericType, T: NumericType) -> NumericType:
        """Auxiliary method implementing formula for non-dimensional cohesion."""
        if isinstance(T, pp.ad.AdArray):
            return T ** (-2) * a * p / R_IDEAL**2
        else:
            return a * p / (R_IDEAL**2 * T**2)

    @staticmethod
    def _B(b: NumericType, p: NumericType, T: NumericType) -> NumericType:
        """Auxiliary method implementing formula for non-dimensional covolume."""
        if isinstance(T, pp.ad.AdArray):
            return T ** (-1) * b * p / (R_IDEAL / 1000.0)
        else:
            return b * p / ((R_IDEAL / 1000.0) * T)

    # TODO
    def _kappa(self, p: NumericType, T: NumericType, Z: NumericType) -> NumericType:
        """
        Parameters:
            p: Pressure.
            T: Temperature.
            Z: Compressibility factor.

        Returns:
            The thermal conductivity.

        """
        return 1.0

    # TODO
    def _mu(self, p: NumericType, T: NumericType, Z: NumericType) -> NumericType:
        """
        Parameters:
            p: Pressure.
            T: Temperature.
            Z: Compressibility factor.

        Returns:
            The dynamic viscosity.

        """
        return 1.0

    @staticmethod
    def _rho(p: NumericType, T: NumericType, Z: NumericType) -> NumericType:
        """Auxiliary function implementing the formula for density."""
        return Z ** (-1) * (p) / (T * R_IDEAL) * PRESSURE_SCALE / ENERGY_SCALE

    @staticmethod
    def _v(p: NumericType, T: NumericType, Z: NumericType) -> NumericType:
        """Auxiliary function implementing the formula for volume."""
        return Z * (T * R_IDEAL) / (p) * ENERGY_SCALE / PRESSURE_SCALE

    @staticmethod
    def _h_dep(
        T: NumericType,
        Z: NumericType,
        a: NumericType,
        dT_a: NumericType,
        b: NumericType,
        B: NumericType,
    ) -> NumericType:
        """Auxiliary function for computing the enthalpy departure function."""
        return (
            1
            / np.sqrt(8)
            * (dT_a * T - a)
            / b
            * pp.ad.log((Z + (1 + np.sqrt(2)) * B) / (Z + (1 - np.sqrt(2)) * B))
            + (Z - 1) * T * R_IDEAL
        )

    @staticmethod
    def _g_dep(A: NumericType, B: NumericType, Z: NumericType):
        """Auxiliary function for computing the Gibbs departure function."""
        return pp.ad.log(pp.ad.maximum(Z - B, 0.0 * B + 1.0e-8)) - pp.ad.log(
            (Z + (1 + np.sqrt(2)) * B) / (Z + (1 - np.sqrt(2)) * B)
        ) * A / B / np.sqrt(8)

    @staticmethod
    def _g_ideal(T: NumericType, X: list[NumericType]) -> NumericType:
        """Auxiliary function to compute the ideal part of the Gibbs energy."""
        return safe_sum([x * pp.ad.log(x) for x in X]) * T * R_IDEAL

    @staticmethod
    def _phi_i(
        Z: NumericType,
        A_i: NumericType,
        A: NumericType,
        B_i: NumericType,
        B: NumericType,
        a: NumericType,
    ) -> NumericType:
        """Auxiliary method implementing the formula for the fugacity coefficient."""
        log_phi_i = (
            (Z - 1) / B * B_i
            - pp.ad.log(pp.ad.maximum(Z - B, 0.0 * B + 1.0e-8)) # Find a better way to instance of null NumericType
            - A
            / (B * np.sqrt(8))
            * (A_i / a - B ** (-1) * B_i)
            * pp.ad.log((Z + (1 + np.sqrt(2)) * B) / (Z + (1 - np.sqrt(2)) * B))
        )
        return pp.ad.exp(log_phi_i)

    @staticmethod
    def _dxi_Z(
        T: NumericType,
        rho: NumericType,
        a: NumericType,
        b: NumericType,
        b_i: NumericType,
        dxi_a: NumericType,
    ) -> NumericType:
        """Auxiliary function implementing the derivative of the compressibility factor
        w.r.t. molar fraction ``x_i``."""
        d = 1 + 2 * rho * b - (rho * b) ** 2
        return rho * b_i / (1 - rho * b) ** 2 + (
            rho * a * (2 * rho * b_i + 2 * rho**2 * b * b_i) / (d**2 * T * R_IDEAL)
            - rho * dxi_a / (d * T * R_IDEAL)
        )

    @staticmethod
    def _Z_polynom(Z: NumericType, A: NumericType, B: NumericType) -> NumericType:
        """Auxiliary method implementing the compressibility polynomial."""
        return (
            Z**3
            + Z**2 * (B - 1)
            + Z * (A - 2 * B - 3 * B**2)
            - (A * B - B**3 - B**2)
        )

    def _Z(
        self, A: NumericType, B: NumericType, apply_smoother: bool = False
    ) -> tuple[NumericType, NumericType]:
        """Auxiliary method to compute the compressibility factor based on Cardano
        formulas.

        Parameters:
            A: Non-dimensional cohesion.
            B: Non-dimensional covolume.
            apply_smoother: ``default=False``

                Flag to apply smoothing procedure.

        Returns:
            Returns two roots. The first one corresponds to the assigned phase label
            :attr:`gaslike`. The second root is the other root.

        """
        shape = None  # will remain None if input is not ad
        # to determine the number of values
        n_a = None
        n_b = None

        # Avoid sparse efficiency warnings and make indexable
        if isinstance(A, pp.ad.AdArray):
            shape = A.jac.shape
            n_a = len(A.val)
            A.jac = A.jac.tolil()
        elif isinstance(A, numbers.Real):
            A = np.array([A])
            n_a = 1
        else:
            n_a = len(A)
        if isinstance(B, pp.ad.AdArray):
            shape = B.jac.shape
            n_b = len(B.val)
            B.jac = B.jac.tolil()
        elif isinstance(B, numbers.Real):
            B = np.array([B])
            n_b = 1
        else:
            n_b = len(B)

        n = np.max((n_a, n_b))  # determine the number of vectorized values

        # the coefficients of the compressibility polynomial
        c0 = pp.ad.power(B, 3) + pp.ad.power(B, 2) - A * B
        c1 = A - 2 * B - 3 * pp.ad.power(B, 2)
        c2 = B - 1

        # the coefficients of the reduced polynomial (elimination of 2-monomial)
        r = c1 - pp.ad.power(c2, 2) / 3
        q = 2 / 27 * pp.ad.power(c2, 3) - c2 * c1 / 3 + c0

        # discriminant to determine the number of roots
        delta = pp.ad.power(q, 2) / 4 + pp.ad.power(r, 3) / 27

        if shape:
            Z_L = pp.ad.AdArray(np.zeros(n), sps.lil_matrix(shape))
            Z_G = pp.ad.AdArray(np.zeros(n), sps.lil_matrix(shape))
        else:
            Z_L = np.zeros(n)
            Z_G = np.zeros(n)

        # an indicater where the root is extended
        self.is_extended = np.zeros(n, dtype=bool)

        ### CLASSIFYING REGIONS
        # NOTE: The logical comparisons are a bit awkward for compatibility reasons with
        # AD-arrays
        # identify super-critical line
        self.is_supercritical = B >= self.B_CRIT / self.A_CRIT * A
        # identify approximated sub pseudo-critical line (includes widom line extension)
        # self.is_sub_pseudo_critical = B <= (self.B_CRIT / (self.A_CRIT - (7.0/40.0))) * (A - (7.0/40.0))
        self.is_sub_pseudo_critical = B <= self.B_CRIT + 0.7 * 0.3381965009398633 * (A - self.A_CRIT)

        # At A,B=0 we have 2 real roots, one with multiplicity 2
        zero_point = np.logical_and(
            np.logical_and(A >= -self.eps, A <= self.eps),
            np.logical_and(B >= -self.eps, B <= self.eps),
        )
        # The critical point is known to be a triple-point
        critical_point = np.logical_and(
            np.logical_and(A >= self.A_CRIT - self.eps, A <= self.A_CRIT + self.eps),
            np.logical_and(B >= self.B_CRIT - self.eps, B <= self.B_CRIT + self.eps),
        )
        # rectangle with upper right corner at (Ac,Bc) and lower left corner at 0
        # with tolerance
        acbc_rect = np.logical_and(
            np.logical_and(A > self.eps, A < self.A_CRIT - self.eps),
            np.logical_and(B > self.eps, B < self.B_CRIT - self.eps),
        )
        # subcritical triangle in the acbc rectangle
        subc_triang = np.logical_and(np.logical_not(self.is_supercritical), acbc_rect)

        # discriminant of zero indicates triple or two real roots with multiplicity
        degenerate_region = np.logical_and(delta >= -self.eps, delta <= self.eps)

        double_root_region = np.logical_and(
            degenerate_region, np.logical_or(r < -self.eps, r > self.eps)
        )
        triple_root_region = np.logical_and(
            degenerate_region, np.logical_and(r >= -self.eps, r <= self.eps)
        )

        one_root_region = delta > self.eps
        three_root_region = delta < -self.eps

        # sanity check that every cell/case is covered
        assert np.all(
            np.logical_or.reduce(
                [
                    one_root_region,
                    triple_root_region,
                    double_root_region,
                    three_root_region,
                ]
            )
        ), "Uncovered cells/rows detected in PR root computation."

        # sanity check that the regions are mutually exclusive
        # this array must have 1 in every entry for the test to pass
        trues_per_row = np.vstack(
            [one_root_region, triple_root_region, double_root_region, three_root_region]
        ).sum(axis=0)
        trues_check = np.ones(n, dtype=trues_per_row.dtype)
        assert np.all(
            trues_check == trues_per_row
        ), "Regions with different root scenarios overlap."

        ### COMPUTATIONS IN THE ONE-ROOT-REGION
        # Missing real root is replaced with conjugated imaginary roots
        if np.any(one_root_region):
            r_ = r[one_root_region]
            q_ = q[one_root_region]
            delta_ = delta[one_root_region]
            c2_ = c2[one_root_region]

            # delta has only positive values in this case by logic
            t = -q_ / 2 + pp.ad.sqrt(delta_)

            # t_1 might be negative, in this case we must choose the real cubic root
            # by extracting cbrt(-1), where -1 is the real cubic root.
            im_cube = t < 0.0
            if np.any(im_cube):
                t[im_cube] = t[im_cube] * (-1)
                u = pp.ad.cbrt(t)
                u[im_cube] = u[im_cube] * (-1)
            else:
                u = pp.ad.cbrt(t)

            # TODO In rare, un-physical areas of A,B, u can become zero,
            # causing infinity here, e.g.
            # A = 0.3620392380873223
            # B = -0.4204815080014268
            # this should never happen in physical simulations,
            # but I note it here nevertheless - VL
            real_part = u - r_ / (u * 3)
            z_1 = real_part - c2_ / 3  # Only real root, always greater than B

            # real part of the conjugate imaginary roots
            # used for extension of vanished roots
            # w = -real_part / 2 - c2_ / 3 + 2.0 * B + self.B_CRIT
            w = (1 - B - z_1) / 2 + B + self.B_CRIT
            # w = (1 - B - z_1) / 2

            extension_is_bigger = self.is_sub_pseudo_critical #z_1 < w

            # correction = ~(acbc_rect[one_root_region])
            # w[correction] = z_1[correction]
            # extension_is_bigger[correction] = False

            # slice indices such that w is the smaller root
            z_1_small = z_1[extension_is_bigger]
            z_1[extension_is_bigger] = w[extension_is_bigger]
            w[extension_is_bigger] = z_1_small

            Z_L[one_root_region] = w
            Z_G[one_root_region] = z_1

            # Store flag where the extended root was used
            if self.gaslike:
                self.is_extended[one_root_region] = extension_is_bigger
            else:
                self.is_extended[one_root_region] = ~extension_is_bigger

        ### COMPUTATIONS IN THE THREE-ROOT-REGION
        # compute all three roots, label them (smallest=liquid, biggest=gas)
        # optionally smooth them
        if np.any(three_root_region):
            r_ = r[three_root_region]
            q_ = q[three_root_region]
            c2_ = c2[three_root_region]

            # compute roots in three-root-region using Cardano formula,
            # Casus Irreducibilis
            t_2 = pp.ad.arccos(-q_ / 2 * pp.ad.sqrt(-27 * pp.ad.power(r_, -3))) / 3
            t_1 = pp.ad.sqrt(-4 / 3 * r_)

            z3 = t_1 * pp.ad.cos(t_2) - c2_ / 3
            z2 = -t_1 * pp.ad.cos(t_2 + np.pi / 3) - c2_ / 3
            z1 = -t_1 * pp.ad.cos(t_2 - np.pi / 3) - c2_ / 3

            # Smoothing procedure only valid in the sub-critical area, where the three
            # roots are positive and bound from below by B
            smoothable = subc_triang[three_root_region]
            if apply_smoother and np.any(smoothable):
                z1_s, z3_s = root_smoother(z1, z2, z3, self.smoothing_factor)

                z3[smoothable] = z3_s[smoothable]
                z1[smoothable] = z1_s[smoothable]

            # assert roots are ordered by size
            assert np.all(z1 <= z2) and np.all(
                z2 <= z3
            ), "Roots in three-root-region improperly ordered."

            Z_L[three_root_region] = z1
            Z_G[three_root_region] = z3

        # we put computations in the triple and double root region at the end
        # as corrective features.

        ### COMPUTATIONS IN TRIPLE ROOT REGION
        # The critical point is known to be a triple root
        # Use logical or to include unknown triple points, but that should not happen
        # NOTE In the critical point, the roots are unavoidably equal
        region = np.logical_or(triple_root_region, critical_point)
        if np.any(region):
            c2_ = c2[region]

            z = -c2_ / 3

            assert np.all(
                z > B[region]
            ), "Triple-roots violating the lower physical bound B detected."

            Z_L[region] = z
            Z_G[region] = z

        ### COMPUTATIONS IN DOUBLE ROOT REGION
        # The point A,B = 0 is known to be such a point
        region = np.logical_or(double_root_region, zero_point)
        if np.any(region):
            r_ = r[region]
            q_ = q[region]
            c2_ = c2[region]

            u = 3 / 2 * q_ / r_

            z_1 = 2 * u - c2_ / 3
            z_23 = -u - c2_ / 3

            # to avoid indexing issues
            if isinstance(z_1, numbers.Real):
                z_1 = np.array([z_1])
                z_23 = np.array([z_23])

            # choose bigger root as gas like
            # theoretically they should strictly be different, otherwise it would be
            # the three root case
            double_is_bigger = z_23 > z_1

            # exchange values such that z_1 is the bigger root
            z_1_small = z_1[double_is_bigger]
            z_1[double_is_bigger] = z_23[double_is_bigger]
            z_23[double_is_bigger] = z_1_small

            Z_L[region] = z_23
            Z_G[region] = z_1

        # Correct the smaller root if it violates the lower bound B
        correction = Z_L <= B
        if np.any(correction):
            Z_L[correction] = B[correction] + self.eps

        # assert physical meaningfulness
        # assert np.all(
        #     Z_L > B
        # ), "Liquid root violates lower physical bound given by covolume B."
        # assert gas root is bigger than liquid root
        # assert np.all(
        #     Z_G >= Z_L
        # ), "Liquid root violates upper physical bound given by gas root."

        # convert Jacobians to csr
        if isinstance(Z_L, pp.ad.AdArray):
            Z_L.jac = Z_L.jac.tocsr()
        if isinstance(Z_G, pp.ad.AdArray):
            Z_G.jac = Z_G.jac.tocsr()

        if self.gaslike:
            return Z_G, Z_L
        else:
            return Z_L, Z_G
