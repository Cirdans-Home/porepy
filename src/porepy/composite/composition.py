"""This module contains a class representing a multiphase multicomponent mixture, here
denoted as *composition* using the unified formulation for phase stability and split
calculations.

The base class is a starting point to derive custom compositions using an
equations of state, which must yield formulae for phase densities and specific
enthalpies. As of now, only those two thermodynamic properties of phases are relevant.

Fractional values in the compositional framework are set and initiated by respective
classes.
The uses has to ensure that values for secondary variables are set prior to using
a composition class or calling a flash. This involves setting values for

- pressure
- temperature
- (specific) enthalpy
- feed fraction per component

The thermodynamic variables can be accessed using
:func:`~porepy.composite.composition.Composition.p`,
:func:`~porepy.composite.composition.Composition.T` and
:func:`~porepy.composite.composition.Composition.h`.

Feed fractions variables can be accessed using
:func:`~porepy.composite.component.Component.fraction` of
:class:`~porepy.composite.component.Component`.

Phases have to be modelled for each composition class using the respective EoS,
see :class:`~porepy.composite.phase.Phase`.

Warning:
    As of now it is recommended to use more than one component.

    Due to the phase rule ``F=C-P+2``, the thermodynamic degree of freedom reduced to 1
    if ``C==``, causing the unified formulation to lose its injectivity.
    This leads to a potentially singular Jacobian of the system.

"""
from __future__ import annotations

import abc
from typing import Generator, Literal, Optional

import numpy as np
import scipy.sparse as sps

import porepy as pp

from ._composite_utils import VARIABLE_SYMBOLS
from .component import Component, Compound
from .phase import Phase

__all__ = ["Composition"]

FlashSystemDict = dict[
    Literal["equations", "primary_vars", "secondary_vars"], list[str]
]
"""A type alias for subsystem dictionaries which contain:

- 'equations': A list of names of equations belonging to this subsystem.
- 'primary_vars': A list of names of primary variables in this subsystem.
- 'secondary_vars': A list of names of secondary variables in this subsystem.

"""


class Composition(abc.ABC):
    """Base class for all compositions with multiple components in the unified setting.

    This class can be used to program composition classes which implement a specific
    equations of state.

    Child classes have to implement their own phases and fugacities.
    The equations in the unified flash procedure are set by this class.

    Notes:
        - The first phase is treated as the reference phase: its molar fraction will not
          be part of the primary variables.
        - The first, added component is set as reference component: its mass
          conservation will not be part of the flash equations.
        - Choice of reference phase and component influence the choice of equations and
          variables, keep that in mind. It might have numeric implications.

    The secondary variables are:

        - pressure,
        - specific enthalpy of the mixture,
        - (p-T flash) temperature of the mixture,
        - feed fractions per component,
        - volumetric phase fractions (saturations),
        - **molar fraction of reference phase (eliminated by unity)**.

    Primary variables are:

        - molar phase fractions except for reference phase,
        - molar component fractions in a phase,
        - (p-h flash) temperature of the mixture.

    Parameters:
        ad_system: ``default=None``

            If given, this class will use the AD system and the respective
            mixed-dimensional domain to represent all involved variables cell-wise in
            each subdomain.

            If not given (None), a single-cell domain and respective AD system are
            created.

    """

    def __init__(self, ad_system: Optional[pp.ad.EquationSystem] = None) -> None:

        if ad_system is None:
            sg = pp.CartGrid([1, 1], [1, 1])
            mdg = pp.MixedDimensionalGrid()
            mdg.add_subdomains(sg)
            mdg.compute_geometry()

            ad_system = pp.ad.EquationSystem(mdg)  # type: ignore

        subdomains = ad_system.mdg.subdomains()

        # state variables
        self._p: pp.ad.MixedDimensionalVariable = ad_system.create_variables(
            self.p_name,
            subdomains=subdomains
        )
        """Pressure variable used for the thermodynamic state."""
        self._h: pp.ad.MixedDimensionalVariable = ad_system.create_variables(
            self.h_name,
            subdomains=subdomains
        )
        """(Specific) enthalpy variable used for the thermodynamic state."""
        self._T: pp.ad.MixedDimensionalVariable = ad_system.create_variables(
            self.T_name,
            subdomains=subdomains
        )
        """Temperature variable used for the thermodynamic state."""

        # modelled phases and components
        self._components: list[Component] = list()
        """A list containing all modelled components."""
        self._phases: list[Phase] = list()
        """A list containing all modelled phases.

        To be created in the constructor of child classes.

        """

        # (parts of) names of equations in the flash subsystem
        self._mass_constraint: str = "mass_constraint"
        """Used to name mass balance operators"""
        self._enthalpy_constraint: str = "enthalpy_constraint"
        """Used to name the enthalpy constraint operator in the p-h flash."""
        self._equilibrium: str = "equilibrium"
        """Used to name the equilibrium equations per component per phase."""

        ### PUBLIC

        self.ad_system: pp.ad.EquationSystem = ad_system
        """The AD system passed at instantiation."""

        self.pT_subsystem: FlashSystemDict = dict()
        """A dictionary representing the subsystem for the p-T flash.

        The equations of the p-T subsystem are:

        - mass constraint per component, except reference component
          (``num_components - 1``). If only one component is present, one equation is
          set up.
        - equilibrium equations per component, between each phase and the reference
          phase
          (``num_components * (num_phases - 1)``)

        The primary variables are:

        - molar phase fraction, except reference phase (``num_phases - 1``)
        - fractions of components in phases (``num_components * num_phases``)

        """

        self.ph_subsystem: FlashSystemDict = dict()
        """A dictionary representing the subsystem for the p-h flash.

        Additionally to the information in :data:`pT_subsystem`, the p-h subsystem
        contains

        - enthalpy constraint (1 equation)
        - temperature (1 primary variable)

        """

        self.fugacity_coeffs: dict[Component, dict[Phase, pp.ad.Operator]] = dict()
        """A nested dictionary containing an operator representing the fugacity
        coefficients for a given ``[component][phase]``.

        This dictionary must be created in :meth:`set_fugacities`, which in return
        is called during :meth:`initialize`.

        """

    def __str__(self) -> str:
        """Returns string representation of the composition,
        with information about present components.
        """
        out = f"Composition with {self.num_components} components:"
        for component in self.components:
            out += f"\n\t{component.name}"
        out += f"\nand {self.num_phases} phases:"
        for phase in self.phases:
            out += f"\n\t{phase.name}"
        return out

    ### Thermodynamic state and properties ---------------------------------------------

    @property
    def p_name(self) -> str:
        """Name of the pressure variable."""
        return VARIABLE_SYMBOLS["pressure"]

    @property
    def p(self) -> pp.ad.MixedDimensionalVariable:
        """The pressure variable for the thermodynamic state.

        The values are assumed to represent values at equilibrium and are therefore
        constant during any flash procedure.

        | Math. Dimension:        scalar
        | Phys. Dimension:        [MPa] = [MN / m^2]


        Returns:
            The primary variable ``p`` on the whole domain (cell-wise).

        """
        return self._p

    @property
    def h_name(self) -> str:
        """Name of the enthalpy variable."""
        return VARIABLE_SYMBOLS["enthalpy"]

    @property
    def h(self) -> pp.ad.MixedDimensionalVariable:
        """The specific molar enthalpy variable for the thermodynamic state.

        For the isenthalpic flash, the values are assumed to represent values at
        equilibrium.
        For the isothermal flash, the enthalpy changes based on the results
        (composition) and should be evaluated afterwards using
        :meth:`evaluate_specific_enthalpy`.

        | Math. Dimension:        scalar
        | Phys. Dimension:        [kJ / mol / K]

        Returns:
            The primary variable ``h`` on the whole domain (cell-wise).

        """
        return self._h

    @property
    def T_name(self) -> str:
        """Name of the temperature variable."""
        return VARIABLE_SYMBOLS["temperature"]

    @property
    def T(self) -> pp.ad.MixedDimensionalVariable:
        """The temperature variable for the thermodynamic state.

        For the isothermal flash, the values are assumed to represent values at
        equilibrium.
        For the isenthalpic flash, the temperature varies and depends on the enthalpy
        and the composition. Its values are determined by the isenthalpic flash
        procedure.

        | Math. Dimension:        scalar
        | Phys. Dimension:        [K]


        Returns:
            The primary variable ``T`` on the whole domain (cell-wise).

        """
        return self._T

    def density(
        self, prev_time: bool = False, eliminate_ref_phase: bool = True
    ) -> pp.ad.Operator | Literal[0]:
        """The molar density of the mixture.

        | Math. Dimension:        scalar
        | Phys. Dimension:        [mol / REV]

        Parameters:
            prev_time: Indicator to use values from the previous time step.
            eliminate_ref_phase: ``default=True``

                If True, the saturation of the reference phase is eliminated by unity.

                If False, the saturation variable of the reference phase is used.

        Returns:
            An AD operator representing the molar mixture density depending on
            the saturation variable and phase densities.
            The phase densities are called using :meth:`p` and :meth:`T`.

        """
        # creating a list of saturation-weighted phase densities
        # If the value from the previous time step (STATE) is requested,
        # we do so using the functionality of the AD framework

        if eliminate_ref_phase:
            if prev_time:

                p = self.p.previous_timestep()
                T = self.T.previous_timestep()
                rho_R = self.reference_phase.density(p, T)
                rho = [rho_R]

                rho += [
                    phase.saturation.previous_timestep() * (phase.density(p, T) - rho_R)
                    for phase in self.phases
                    if phase != self.reference_phase
                ]
            else:
                rho_R = self.reference_phase.density(self.p, self.T)
                rho = [rho_R]

                rho += [
                    phase.saturation * (phase.density(self.p, self.T) - rho_R)
                    for phase in self.phases
                    if phase != self.reference_phase
                ]
        else:
            if prev_time:

                p = self.p.previous_timestep()
                T = self.T.previous_timestep()

                rho = [
                    phase.saturation.previous_timestep() * phase.density(p, T)
                    for phase in self.phases
                ]
            else:
                rho = [
                    phase.saturation * phase.density(self.p, self.T)
                    for phase in self.phases
                ]

        # summing the elements of the list results in the mixture density
        return sum(rho)

    ### Composition Management ---------------------------------------------------------

    @property
    def num_components(self) -> int:
        """Number of components in the composition."""
        return len(self._components)

    @property
    def num_phases(self) -> int:
        """Number of *modelled* phases in the composition."""
        return len(self._phases)

    @property
    def num_equilibrium_equations(self) -> int:
        """Number of necessary equilibrium equations for this composition, based on the
        number of added components and modelled phases."""
        return self.num_components * (self.num_phases - 1)

    @property
    def components(self) -> Generator[Component, None, None]:
        """
        Yields:
            Components added to the composition.

        """
        for C in self._components:
            yield C

    @property
    def phases(self) -> Generator[Phase, None, None]:
        """
        Yields:
            Phases modelled by the composition class.

        """
        for P in self._phases:
            yield P

    @property
    def reference_phase(self) -> Phase:
        """Returns the reference phase.

        The molar and volumetric phase fraction of the reference phase can be eliminated
        where applicable.

        """
        # assert the child classes has a non-empty list of phases
        assert self._phases
        return self._phases[0]

    @property
    def reference_component(self) -> Component | None:
        """Returns the reference component.

        The first component added to the mixture is set as reference.

        The mass balance equation for the reference component is eliminated by unity
        in the unified framework.

        """
        if self._components:
            return self._components[0]

    def add_component(self, component: Component | list[Component]) -> None:
        """Adds one or multiple components to the composition.

        Components must be added before the composition is initialized.

        Parameters:
            component: One or multiple components to be added to this mixture.

        Raises:
            ValueError: If the component was instantiated using a different AD system
                than the one used for this composition.

        """
        if isinstance(component, Component):
            component = [component]  # type: ignore

        added_components = [comp.name for comp in self._components]

        for comp in component:
            if comp.name in added_components:
                # already added components are skipped
                continue

            # sanity check when using the AD framework
            if self.ad_system != comp.ad_system:
                raise ValueError(
                    f"Component '{comp.name}' instantiated with a different AD system."
                )

            # add component
            self._components.append(comp)
            # add component to all phases
            for phase in self.phases:
                phase.add_component(comp)

    def initialize(self) -> None:
        """Initializes the flash equations for this mixture based on the added
        components.

        This is the last step before any flash method should be called.

        It creates the system of equations and the two subsystems for the p-T and
        p-h flash.

        Note:
            :meth:`set_fugacities`  is called here before the equilibrium equations
            are set as a last step.

        Raises:
            AssertionError: If the mixture is empty (no components).
            AssertionError: If less than 2 phases are modelled.

        """
        # assert non-empty mixture
        assert self.num_components >= 1, "No components added to mixture."
        # assert there are at least 2 phases modelled
        assert self.num_phases >= 2, "Composition modelled with only one phase."

        # allocating subsystems
        equations: dict[str, pp.ad.Operator] = dict()
        pT_subsystem: FlashSystemDict = {
            "equations": list(),
            "primary_vars": list(),
            "secondary_vars": list(),
        }
        ph_subsystem: FlashSystemDict = {
            "equations": list(),
            "primary_vars": list(),
            "secondary_vars": list(),
        }
        self._set_subsystem_vars(ph_subsystem, pT_subsystem)

        ### Mass conservation equations
        # if only one component, its mass balance is added
        if self.num_components == 1:
            name = f"{self._mass_constraint}_{self.reference_component.name}"
            equation = self.get_mass_conservation_for(self.reference_component, True)
            equations.update({name: equation})
            pT_subsystem["equations"].append(name)
            ph_subsystem["equations"].append(name)
        # if multiple components, exclude the mass balance for the reference component,
        # since it can be recovered by linear combination and unity of fractions
        else:
            for component in self.components:
                if component != self.reference_component:
                    name = f"{self._mass_constraint}_{component.name}"
                    equation = self.get_mass_conservation_for(component, True)
                    equations.update({name: equation})
                    pT_subsystem["equations"].append(name)
                    ph_subsystem["equations"].append(name)

        ### enthalpy constraint for p-H flash
        equation = self.get_enthalpy_constraint(True)
        equations.update({self._enthalpy_constraint: equation})
        ph_subsystem["equations"].append(self._enthalpy_constraint)

        ### setting fugacities and and equilibrium equation
        self.set_fugacities()
        ref_phase = self.reference_phase

        for comp in self.components:
            for phase in self.phases:
                if phase != ref_phase:

                    equation = self.get_equilibrium_equation(comp, phase)
                    name = (
                        f"{self._equilibrium}_{comp.name}_{phase.name}_{ref_phase.name}"
                    )

                    equations.update({name: equation})

                    pT_subsystem["equations"].append(name)
                    ph_subsystem["equations"].append(name)

        # adding equations to AD system
        # every equation in the unified flash is a cell-wise scalar equation
        for name, equ in equations.items():
            equ.set_name(name)
            self.ad_system.set_equation(
                equ,
                grids=self.ad_system.mdg.subdomains(),
                equations_per_grid_entity={"cells": 1}
            )

        # storing references to the subsystems
        self.pT_subsystem = pT_subsystem
        self.ph_subsystem = ph_subsystem

    def _set_subsystem_vars(
        self,
        ph_subsystem: FlashSystemDict,
        pT_subsystem: FlashSystemDict,
    ) -> None:
        """Auxiliary function to set the variables in respective subsystems."""

        ### FLASH SECONDARY VARIABLES
        # pressure is always a secondary var in the flash
        pT_subsystem["secondary_vars"].append(self.p_name)
        ph_subsystem["secondary_vars"].append(self.p_name)
        # enthalpy is always a secondary var in the flash
        ph_subsystem["secondary_vars"].append(self.h_name)
        pT_subsystem["secondary_vars"].append(self.h_name)
        # Temperature is only secondary in the p-T flash because it is fixed
        # It varies in the p-h flash.
        pT_subsystem["secondary_vars"].append(self.T_name)
        # feed fractions are always secondary vars
        for component in self.components:
            pT_subsystem["secondary_vars"].append(component.fraction_name)
            ph_subsystem["secondary_vars"].append(component.fraction_name)
        # saturations are always secondary vars
        for phase in self.phases:
            pT_subsystem["secondary_vars"].append(phase.saturation_name)
            ph_subsystem["secondary_vars"].append(phase.saturation_name)
        # molar fraction of the reference phase is always a secondary var
        pT_subsystem["secondary_vars"].append(self.reference_phase.fraction_name)
        ph_subsystem["secondary_vars"].append(self.reference_phase.fraction_name)
        # solute fractions in compounds are always secondary vars in the flash
        for component in self.components:
            if isinstance(component, Compound):
                for solute in component.solutes:
                    solute_fraction_name = component.solute_fraction_name(solute)
                    pT_subsystem["secondary_vars"].append(solute_fraction_name)
                    ph_subsystem["secondary_vars"].append(solute_fraction_name)

        ### FLASH PRIMARY VARIABLES
        # for the p-h flash, T is an additional var
        ph_subsystem["primary_vars"].append(self.T_name)
        # phase fractions
        for phase in self.phases:
            if phase != self.reference_phase:
                pT_subsystem["primary_vars"].append(phase.fraction_name)
                ph_subsystem["primary_vars"].append(phase.fraction_name)
            # phase composition
            for component in phase:
                var_name = phase.fraction_of_component_name(component)
                pT_subsystem["primary_vars"].append(var_name)
                ph_subsystem["primary_vars"].append(var_name)

    ### Subsystem assembly method ------------------------------------------------------

    def linearize_subsystem(
        self,
        flash_type: Literal["isenthalpic", "isothermal"],
        other_vars: Optional[list[str]] = None,
        other_eqns: Optional[list[str]] = None,
        state: Optional[np.ndarray] = None,
    ) -> tuple[sps.spmatrix, np.ndarray]:
        """Assembles the linearized system of respective flash type.

        This method is introduced here such that it can be called by the
        :class:`~porepy.composite.flash.Flash`.

        Also, child classes can override this method to implement additional steps
        necessary for respective mixture models.

        This method allows additionally for including other variables and equations,
        which will be straight forward passed to the AD system. I.e., if there are any
        issues, the AD system will raise respective errors (or not...).

        Parameters:
            flash_type:
                - ``'isenthalpic'``: Assembles the subsystem stored in
                  :data:`ph_subsystem`.
                - ``'isothermal'``: Assembles the subsystem stored in
                  :data:`pT_subsystem`.
            other_vars: ``default=None``

                A list of additional variable names to be included as columns.

                This is meant for flash algorithms,
                which introduce additional variables.
            other_eqns: ``default=None``

                A list of additional equations (names) to be included as rows.

                This is meant for flash algorithms,
                which introduce additional equations.
            state: ``default=None``

                A state vector w.r.t. which the system should be assembled
                (see :meth:`~porepy.numerics.ad.ad_system.assemble_subsystem`).

        Raises:
            ValueError: If ``flash_type`` unknown.

        """

        if flash_type == "isenthalpic":
            equations = self.ph_subsystem["equations"]
            variables = self.ph_subsystem["primary_vars"]
        elif flash_type == "isothermal":
            equations = self.pT_subsystem["equations"]
            variables = self.pT_subsystem["primary_vars"]
        else:
            raise ValueError(f"Unknown flash type {flash_type}.")

        return self.ad_system.assemble_subsystem(
            equations + other_eqns if other_eqns else equations,
            variables + other_vars if other_vars else variables,
            state=state,
        )

    ### Model equations ----------------------------------------------------------------

    def get_mass_conservation_for(
        self,
        component: Component,
        eliminate_ref_phase: bool = True,
        normalize_phase_composition: bool = False,
    ) -> pp.ad.Operator:
        """Returns an operator representing the mass balance in form of the definition
        of the overall component fraction (mass conservation) for a component.

            `` y_R = 1 - sum_{e != R} y_e``,
            ``z_c - sum_e y_e * chi_ce = 0``,
            ``z_c - chi_cR - sum_{e != R} y_e * (chi_ce - chi_cR) = 0``.

        Parameters:
            component: a component in this composition
            eliminate_ref_phase: ``default=True``

                If True, the reference phase molar fraction is eliminated by unity.
            normalize_phase_composition: ``default=False``

                If True, uses the normalized phase compositions, instead of the extended
                fractions.

        Returns:
            AD operator representing the left-hand side of the equation (rhs=0).

        """
        # z_c
        equation = component.fraction - self.get_component_fraction_by_definition(
            component, eliminate_ref_phase, normalize_phase_composition
        )
        # if eliminate_ref_phase:
        #     if normalize_phase_composition:
        #         chi_cR = self.reference_phase.normalized_fraction_of_component(component)
        #     else:
        #         chi_cR = self.reference_phase.fraction_of_component(component)
        #     # z_c  - chi_cR
        #     equation -= chi_cR
        #     # - sum_{e != R} y_e * (chi_ce - chi_cR)
        #     for phase in self.phases:
        #         if phase != self.reference_phase:
        #             if normalize_phase_composition:
        #                 chi_ce = phase.normalized_fraction_of_component(component)
        #             else:
        #                 chi_ce = phase.fraction_of_component(component)
        #             equation -= phase.fraction * (chi_ce - chi_cR)
        # else:
        #     for phase in self.phases:
        #         if normalize_phase_composition:
        #                 chi_ce = phase.normalized_fraction_of_component(component)
        #             else:
        #                 chi_ce = phase.fraction_of_component(component)
        #         equation -= phase.fraction * chi_ce

        return equation

    def get_component_fraction_by_definition(
        self,
        component: Component,
        eliminate_ref_phase: bool = True,
        normalize_phase_composition: bool = False,
    ) -> pp.ad.Operator:
        """Returns an operator representing the definition of the overall component
        fraction (mass conservation) for a component.

            `` y_R = 1 - sum_{e != R} y_e``,
            ``z_c = sum_e y_e * chi_ce``,
            ``z_c = chi_cR + sum_{e != R} y_e * (chi_ce - chi_cR)``.

        Parameters:
            component: a component in this composition
            eliminate_ref_phase: ``default=True``

                If True, the reference phase molar fraction is eliminated by unity.
            normalize_phase_composition: ``default=False``

                If True, uses the normalized phase compositions, instead of the extended
                fractions.

        Returns:
            AD operator representing the right-hand side of the equation.

        """
        if eliminate_ref_phase:
            if normalize_phase_composition:
                equation = sum(
                    [
                        phase.fraction
                        * phase.normalized_fraction_of_component(component)
                        if phase != self.reference_phase
                        else self.get_reference_phase_fraction_by_unity()
                        * phase.normalized_fraction_of_component(component)
                        for phase in self.phases
                    ]
                )
            else:
                equation = sum(
                    [
                        phase.fraction * phase.fraction_of_component(component)
                        if phase != self.reference_phase
                        else self.get_reference_phase_fraction_by_unity()
                        * phase.fraction_of_component(component)
                        for phase in self.phases
                    ]
                )

            return equation
        else:
            if normalize_phase_composition:
                equation = sum(
                    [
                        phase.fraction
                        * phase.normalized_fraction_of_component(component)
                        for phase in self.phases
                    ]
                )
            else:
                equation = sum(
                    [
                        phase.fraction * phase.fraction_of_component(component)
                        for phase in self.phases
                    ]
                )

            return equation

    def get_phase_fraction_unity(self) -> pp.ad.Operator:
        """Returns an equation representing the phase fraction unity

            ``1 - sum_e y_e = 0``.

        Returns:
            AD operator representing the left-hand side of the equation (rhs=0).

        """
        equation = pp.ad.Scalar(1.0)

        for phase in self.phases:
            equation -= phase.fraction

        return equation

    def get_phase_saturation_unity(self) -> pp.ad.Operator:
        """Returns an equation representing the phase fraction unity

            ``1 - sum_e s_e = 0``.

        Returns:
            AD operator representing the left-hand side of the equation (rhs=0).

        """
        equation = pp.ad.Scalar(1.0)

        for phase in self.phases:
            equation -= phase.saturation

        return equation

    def get_reference_phase_fraction_by_unity(self) -> pp.ad.Operator:
        """Returns an equation which expresses the fraction of the reference phase
        by unity

            ``y_R = 1 - sum_{e != R} y_e.

        Returns:
            AD operator representing the right-hand side of the equation.

        """
        equation = pp.ad.Scalar(1.0)
        for phase in self.phases:
            if phase != self.reference_phase:
                # - y_e, where e != R
                equation -= phase.fraction
        # y_R
        return equation

    def get_reference_phase_saturation_by_unity(self) -> pp.ad.Operator:
        """Returns an equation which expresses the saturation of the reference phase
        by unity

            ``s_R = 1 - sum_{e != R} s_e.

        Returns:
            AD operator representing the right-hand side of the equation.

        """
        equation = pp.ad.Scalar(1.0)
        for phase in self.phases:
            if phase != self.reference_phase:
                # - s_e, where e != R
                equation -= phase.saturation
        # s_R
        return equation

    def get_phase_fraction_relation(
        self, phase: Phase, eliminate_ref_phase: bool = True
    ) -> pp.ad.Operator:
        """Returns an operator representing the relation between the molar fraction
        of a phase and its volumetric fraction (saturation).

        The equation includes the unity of saturations, i.e.

            ``y_e = (rho_e * s_e) / (sum_f rho_f s_f)``,
            ``y_e * rho - s_e * rho_e = 0``.

        Parameters:
            phase: a phase in this composition
            eliminate_ref_phase: ``default=True``

                If True, the reference phase saturation and fraction are eliminated
                by unity.

        Returns:
            AD operator representing the left-hand side of the third equation (rhos=0).

        """
        if eliminate_ref_phase:
            rho = self.density(eliminate_ref_phase=True)

            if phase == self.reference_phase:
                # rho * (1 - sum_{e != R} y_e)
                equation = rho * self.get_reference_phase_fraction_by_unity()
                # rho_R * (1 - sum_{e != R} s_e)
                equation -= (
                    phase.density(self.p, self.T)
                    * self.get_reference_phase_saturation_by_unity()
                )
            else:
                equation = (
                    rho * phase.fraction
                    - phase.density(self.p, self.T) * phase.saturation
                )
        else:
            equation = (
                self.density(eliminate_ref_phase=False) * phase.fraction
                - phase.density(self.p, self.T) * phase.saturation
            )

        return equation

    def get_enthalpy_constraint(
        self, eliminate_ref_phase: bool = True
    ) -> pp.ad.Operator:
        """Returns an equation representing the specific molar enthalpy of the mixture,
        based on its definition

            ``y_R = 1 - sum_{e != R} y_e``,
            ``h - sum_e y_e * h_e(p,T) = 0``,
            ``h - h_R - sum_{e != R} y_e * (h_e - h_R) = 0``.

        Used to for the p-h flash as enthalpy constraint (T is an additional variable).

        Parameters:
            eliminate_ref_phase: ``default=True``

                If True, the reference phase fraction is eliminated by unity.

        Returns:
            AD operator representing the left-hand side of the equation (rhs=0).

        """
        if eliminate_ref_phase:
            # enthalpy of reference phase
            h_R = self.reference_phase.specific_enthalpy(self.p, self.T)
            equation = self.h - h_R

            for phase in self.phases:
                if phase != self.reference_phase:
                    equation -= phase.fraction * (
                        phase.specific_enthalpy(self.p, self.T) - h_R
                    )
        else:
            equation = self.h

            for phase in self.phases:
                equation -= phase.fraction * phase.specific_enthalpy(self.p, self.T)

        return equation

    def get_composition_unity_for(self, phase: Phase) -> pp.ad.Operator:
        """Returns an equation representing the unity of the composition for a phase:

            ``1 - sum_c chi_ce = 0``.

        Parameters:
            phase: A phase in this composition.

        Returns:
            AD operator representing the left-hand side of the equation (rhs=0).

        """
        equation = pp.ad.Scalar(1.0)

        for component in phase:
            equation -= phase.fraction_of_component(component)

        return equation

    def get_complementary_condition_for(
        self, phase: Phase
    ) -> tuple[pp.ad.Operator, pp.ad.Operator]:
        """Returns the two complementary equations for a phase

            ``min{y_e, 1 - sum_c chi_ce} = 0``.

        Parameters:
            phase: A phase in this composition.

        Returns:
            Tuple of AD operators representing the left-hand side of the equation
            (rhs=0).

        """
        return (phase.fraction, self.get_composition_unity_for(phase))

    def get_equilibrium_equation(
        self, component: Component, other_phase: Phase
    ) -> pp.ad.Operator:
        """Creates an equilibrium equation for a given ``component`` between
        ``other_phase`` and the reference phase

            ``x_ce * phi_ce - x_cR * phi_cR = 0``,

        where ``e`` is the index for ``other_phase`` and ``R`` for the reference phase.

        Parameters:
            component: A component in this composition.
            phase: A phase other than the reference phase in this composition.

        Raises:
            KeyError: If fugacities were not assigned (see :meth:`set_fugacities`).
            ValueError: If ``other_phase`` is the reference phase.

        Returns:
            AD operator representing the left-hand side of the equation (rhs=0).

        """
        # sanity check
        if other_phase == self.reference_phase:
            raise ValueError(
                "Cannot construct equilibrium equation between reference phase and "
                "itself."
            )

        equation = other_phase.fraction_of_component(component) - (
            self.get_k_value(component, other_phase)
        ) * self.reference_phase.fraction_of_component(component)

        return equation

    def get_k_value(self, component: Component, other_phase: Phase) -> pp.ad.Operator:
        """Once the fugacities are set (see :meth:`set_fugacities`), the k-values
        are obtained w.r.t. the reference phase

            ``x_ce * phi_ce - x_cR * phi_cR = 0``,
            ``x_ce - k_ce * x_cR = 0``,

        where

            ``k_ce = phi_cR / phi_ce``,

        ``e`` is the index of ``other_phase`` and ``R`` is the index of the reference
        phase.

        Parameters:
            component: A component in this mixture.
            other_phase: A phase other than the reference phase in this composition.

        Raises:
            KeyError: If fugacities are not found for given input.

        Returns:
            An operator representing ``k_ce`` in above equations.

        """
        phi_ce = self.fugacity_coeffs[component][other_phase]
        phi_cR = self.fugacity_coeffs[component][self.reference_phase]

        return phi_cR / phi_ce

    @abc.abstractmethod
    def set_fugacities(self) -> None:
        """Abstract method to create fugacity operators for a component in a specific
        phase.

        This method should fill the dictionary :data:`fugacities`.

        Note:
            Equilibrium equations are formulated using fugacities,
            with respect to the reference phase.

        """
        pass
