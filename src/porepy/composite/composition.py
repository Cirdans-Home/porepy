"""Contains a class representing a multiphase multicomponent mixture (composition)."""
from __future__ import annotations

import abc
from typing import Any, Generator, Literal, Optional

import numpy as np
import scipy.sparse as sps

import porepy as pp

from ._composite_utils import VARIABLE_SYMBOLS
from .component import Component, Compound
from .phase import Phase

__all__ = ["Composition"]


class Composition(abc.ABC):
    """Base class for all compositions with multiple components (chemical substances) in the
    unified setting.

    This class can be used to program composition classes which implement specific equations of
    state.
    Child classes have to implement their own phases and equilibrium equations.
    The other equations in the unified flash procedure are set by this class.

    Note:
        - Two flash procedures are implemented: p-T flash and p-h flash
        - The first phase is treated as the reference phase: its molar fraction will not be
          part of the primary variables.
        - The first, added component is set as reference component: its mass conservation will
          not be part of the flash equations.
        - Choice of reference phase and component influence the choice of equations and
          variables, keep that in mind. It might have numeric implications.
        - Equilibrium calculations can be done cell-wise.
          I.e. the computations can be done smarter or parallelized. This is not yet exploited.

    The secondary variables are:

        - pressure,
        - specific enthalpy of the mixture,
        - (p-T flash) temperature of the mixture,
        - feed fractions per component.
        - volumetric phase fractions (saturations)

    Primary variables are:

        - molar phase fractions,
        - molar component fractions in a phase,
        - (p-h flash) temperature of the mixture.

    Warning:
        The phase fraction of the reference phase,
        as well as the normalized phase composition variables
        are neither primary nor secondary. They are evaluated once the procedure converges.
        Keep this in mind when using the composition in other models, e.g. flow.

    The values of all fractional variables are calculated by this class,
    i.e. they do not have to be set externally.

    While the molar fractions are the actual unknowns in the flash procedure, the saturation
    values can be computed once the equilibrium converges using a relation between molar and
    volumetric fractions for phases based on an averaging process for porous media.

    The specific enthalpy can be evaluated directly after a p-T flash.

    Parameters:
        ad_system (optional): If given, this class will use this AD system and the respective
            mixed-dimensional domain to represent all involved variables cell-wise in each
            subdomain.
            If not given (None), a single-cell domain and respective AD system are created.

    """

    def __init__(self, ad_system: Optional[pp.ad.ADSystem] = None) -> None:

        if ad_system is None:
            sg = pp.CartGrid([1, 1], [1, 1])
            mdg = pp.MixedDimensionalGrid()
            mdg.add_subdomains(sg)
            mdg.compute_geometry()

            ad_system = pp.ad.ADSystem(mdg)  # type: ignore

        ### PUBLIC

        self.ad_system: pp.ad.ADSystem = ad_system
        """The AD system passed at instantiation."""

        self.flash_history: list[dict[str, Any]] = list()
        """Contains chronologically stored information about calculated flash procedures."""

        self.flash_tolerance: float = 1e-8
        """Convergence criterion for the flash algorithm."""

        self.max_iter_flash: int = 1000
        """Maximal number of iterations for the flash algorithms."""

        self.ph_subsystem: dict[str, list] = dict()
        """A dictionary representing the subsystem for the p-h flash. Contains information on
        relevant variables and equations.

        """

        self.pT_subsystem: dict[str, list] = dict()
        """A dictionary representing the subsystem for the p-T flash. Contains information on
        relevant variables and equations.

        """

        ### PRIVATE
        # primary variables
        self._p: pp.ad.MergedVariable = ad_system.create_variable(self.p_name)
        self._h: pp.ad.MergedVariable = ad_system.create_variable(self.h_name)
        self._T: pp.ad.MergedVariable = ad_system.create_variable(self.T_name)

        # composition
        self._components: list[Component] = list()
        """A list containing all modelled components."""

        self._phases: list[Phase] = list()
        """A list containing all modelled phases.

        To be created in the constructor of child classes.

        """

        # maximal number of flash history entries (FiFo)
        self._max_history: int = 100
        # names of equations
        self._mass_conservation: str = "flash_mass"
        self._phase_fraction_unity: str = "flash_phase_unity"
        self._complementary: str = "flash_KKT"  # complementary conditions
        self._enthalpy_constraint: str = "flash_h_constraint"  # for p-h flash

        # semi-smooth min operator for KKT condition
        self._ss_min: pp.ad.Operator = pp.ad.SemiSmoothMin()
        # representation of eliminated phase fraction by unity, set in initialization
        self._y_R: pp.ad.Operator

    ### Thermodynamic State -------------------------------------------------------------------

    @property
    def p_name(self) -> str:
        """Returns the name of the pressure variable."""
        return VARIABLE_SYMBOLS["pressure"]

    @property
    def p(self) -> pp.ad.MergedVariable:
        """
        The values are assumed to represent values at equilibrium and are therefore constant
        during any flash procedure.

        | Math. Dimension:        scalar
        | Phys. Dimension:        [MPa] = [MN / m^2]


        Returns:
            the primary variable pressure on the whole domain (cell-wise).

        """
        return self._p

    @property
    def h_name(self) -> str:
        """Returns the name of the enthalpy variable."""
        return VARIABLE_SYMBOLS["enthalpy"]

    @property
    def h(self) -> pp.ad.MergedVariable:
        """
        For the isenthalpic flash, the values are assumed to represent values at equilibrium.
        For the isothermal flash, the enthalpy changes based on the results (composition).

        | Math. Dimension:        scalar
        | Phys. Dimension:        [kJ / mol / K]

        Returns:
            the primary variable specific molar enthalpy on the whole domain (cell-wise).

        """
        return self._h

    @property
    def T_name(self) -> str:
        """Returns the name of the temperature variable."""
        return VARIABLE_SYMBOLS["temperature"]

    @property
    def T(self) -> pp.ad.MergedVariable:
        """
        For the isothermal flash, the values are assumed to represent values at equilibrium.
        For the isenthalpic flash, the temperature varies and depends on the enthalpy and the
        composition.

        | Math. Dimension:        scalar
        | Phys. Dimension:        [K]


        Returns:
            the primary variable temperature on the whole domain (cell-wise).

        """
        return self._T

    def density(
        self, prev_time: bool = False, eliminate_ref_phase: bool = True
    ) -> pp.ad.Operator | Literal[0]:
        """
        | Math. Dimension:        scalar
        | Phys. Dimension:        [mol / REV]

        Parameters:
            prev_time: indicator to use values from the previous time step.
            eliminate_ref_phase (optional): If True, the saturation of the reference phase
                is eliminated by unity.

        Returns:
            Returns the overall molar density of the composition
            given by the saturation-weighted sum of all phase densities.
            The phase densities are computed using the current temperature and pressure
            values.

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

    ### Composition Management ----------------------------------------------------------------

    @property
    def num_components(self) -> int:
        """Number of components in the composition."""
        return len(self._components)

    @property
    def num_phases(self) -> int:
        """Number of **modelled** phases in the composition."""
        return len(self._phases)

    @property
    def num_equilibrium_equations(self) -> int:
        """Number of necessary equilibrium equations for this composition, based on the number
        of added components and modelled phases."""
        return self.num_components * (self.num_phases - 1)

    @property
    def components(self) -> Generator[Component, None, None]:
        """
        Yields:
            components added to the composition.

        """
        for C in self._components:
            yield C

    @property
    def phases(self) -> Generator[Phase, None, None]:
        """
        Yields:
            phases modelled by the composition class.

        """
        for P in self._phases:
            yield P

    @property
    def reference_phase(self) -> Phase:
        """Returns the reference phase, whose molar phase fraction is eliminated by unity."""
        # assert the child classes has a non-empty list of phases
        assert self._phases
        return self._phases[0]

    @property
    def reference_component(self) -> Component | None:
        """Returns the reference component, whose mass balance equation is eliminated
        by unity of feed fractions.

        """
        if self._components:
            return self._components[0]

    def add_component(self, component: list[Component] | Component) -> None:
        """Adds one or multiple components to the composition.

        All modelled components must be added before the composition is initialized.

        Parameters:
            component: one or multiple components to be added to this mixture.

        Raises:
            ValueError: if the component was instantiated using a different AD system than
            the one used for this composition.

        """
        if isinstance(component, Component):
            component = [component]

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
        """Initializes the flash equations for this system, based on the added components.

        This is the last step before a flash method should be called.
        It creates the system of equations and the two subsystems for the p-T and p-h flash.

        Every derived composition class has to override this method and implement the setting
        of class-specific equilibrium equations.

        After a a super-call to ``initialize``, the equations must be set in the AD system
        and stored in respective subsystem dictionaries using the keyword ``'equations'``

        Raises:
            AssertionError: If the mixture is empty (no components).

        """
        # assert non-empty mixture
        assert self.num_components >= 1

        # allocating place for the subsystem
        equations = dict()
        pT_subsystem: dict[str, list] = self._get_subsystem_dict()
        ph_subsystem: dict[str, list] = self._get_subsystem_dict()
        self._set_subsystem_vars(ph_subsystem, pT_subsystem)
        # assert there are phases present
        assert self._phases

        ### reference phase fraction by unity
        self._y_R = self.get_reference_phase_fraction_by_unity()

        ### Mass conservation equations
        for component in self.components:
            # we eliminate the mass balance for the reference component
            # (unity of feed fractions)
            if component != self.reference_component:
                name = f"{self._mass_conservation}_{component.name}"
                equation = self.get_mass_conservation_for(component, True)
                equations.update({name: equation})
                pT_subsystem["equations"].append(name)
                ph_subsystem["equations"].append(name)

        # if only a single component is present, we have to use its mass balance
        if self.num_components == 1:
            name = f"{self._mass_conservation}_{self.reference_component.name}"
            equation = self.get_mass_conservation_for(self.reference_component, True)
            equations.update({name: equation})
            pT_subsystem["equations"].append(name)
            ph_subsystem["equations"].append(name)
        # else we use the whole set of KKT, including reference phase.
        else:
            # setting the KKT condition for the reference phase
            name = f"{self._complementary}_{self.reference_phase}"
            _, lagrange = self.get_complementary_condition_for(self.reference_phase)
            constraint = self.get_reference_phase_fraction_by_unity()

            equation = self._ss_min(constraint, lagrange)
            equations.update({name: equation})
            pT_subsystem["equations"].append(name)
            ph_subsystem["equations"].append(name)

        # NOTE: The Unified flash procedure is not meant for mixtures with one component.
        # Due to the phase rule, the thermodynamic degrees of freedom reduce to one and the
        # whole system becomes tricky to solve. TODO document and explain.

        ### Semi-smooth complementary conditions per phase
        for phase in self.phases:
            if phase == self.reference_phase:
                continue
            name = f"{self._complementary}_{phase.name}"
            constraint, lagrange = self.get_complementary_condition_for(phase)

            # instantiate semi-smooth min in AD form with skewing factor
            equation = self._ss_min(constraint, lagrange)
            equations.update({name: equation})
            pT_subsystem["equations"].append(name)
            ph_subsystem["equations"].append(name)

        ### enthalpy constraint for p-H flash
        equation = self.get_enthalpy_constraint(True)
        equations.update({self._enthalpy_constraint: equation})
        ph_subsystem["equations"].append(self._enthalpy_constraint)

        # adding equations to AD system
        image_info = dict()
        for sd in self.ad_system.dof_manager.mdg.subdomains():
            image_info.update({sd: {"cells": 1}})
        for name, equ in equations.items():
            self.ad_system.set_equation(name, equ, num_equ_per_dof=image_info)
        # storing references to the subsystems
        self.pT_subsystem = pT_subsystem
        self.ph_subsystem = ph_subsystem

    def _get_subsystem_dict(self) -> dict[str, list]:
        """Returns a template for subsystem dictionaries."""
        return {
            "equations": list(),
            "primary_vars": list(),
            "secondary_vars": list(),
        }

    def _set_subsystem_vars(
        self, ph_subsystem: dict[str, list], pT_subsystem: dict[str, list]
    ) -> None:
        """Auxiliary function to set the variables in respective subsystems."""

        ### FLASH SECONDARY VARIABLES
        # pressure is always a secondary var in the flash
        pT_subsystem["secondary_vars"].append(self.p_name)
        ph_subsystem["secondary_vars"].append(self.p_name)
        # for the p-H flash, enthalpy is a secondary var
        ph_subsystem["secondary_vars"].append(self.h_name)
        # for the p-T flash, temperature AND enthalpy are secondary vars,
        # because h can be evaluated for given T and fractions
        pT_subsystem["secondary_vars"].append(self.h_name)
        pT_subsystem["secondary_vars"].append(self.T_name)
        # feed fractions are always secondary vars
        for component in self.components:
            pT_subsystem["secondary_vars"].append(component.fraction_name)
            ph_subsystem["secondary_vars"].append(component.fraction_name)
        # saturations are always secondary vars
        for phase in self.phases:
            pT_subsystem["secondary_vars"].append(phase.saturation_name)
            ph_subsystem["secondary_vars"].append(phase.saturation_name)
        # solute fractions in compounds are always secondary vars in the flash
        for component in self.components:
            if isinstance(component, Compound):
                for solute in component.solutes:
                    solute_fraction_name = component.solute_fraction_name(solute)
                    pT_subsystem["secondary_vars"].append(solute_fraction_name)
                    ph_subsystem["secondary_vars"].append(solute_fraction_name)

        ### FLASH PRIMARY VARIABLES
        # phase fractions
        for phase in self.phases:
            if phase != self.reference_phase:
                pT_subsystem["primary_vars"].append(phase.fraction_name)
                ph_subsystem["primary_vars"].append(phase.fraction_name)
            # reference phase fractions are secondary
            else:
                pT_subsystem["secondary_vars"].append(phase.fraction_name)
                ph_subsystem["secondary_vars"].append(phase.fraction_name)
            # phase composition
            for component in phase:
                var_name = phase.fraction_of_component_name(component)
                pT_subsystem["primary_vars"].append(var_name)
                ph_subsystem["primary_vars"].append(var_name)
        # for the p-h flash, T is an additional var
        ph_subsystem["primary_vars"].append(self.T_name)

    ### other ---------------------------------------------------------------------------------

    def print_last_flash(self) -> None:
        """Prints the result of the last flash calculation."""
        entry = self.flash_history[-1]
        msg = "\nProcedure: %s\n" % (str(entry["flash"]))
        msg += "SUCCESS: %s\n" % (str(entry["success"]))
        msg += "Method: %s\n" % (str(entry["method"]))
        msg += "Iterations: %s\n" % (str(entry["iterations"]))
        msg += "Remarks: %s" % (str(entry["other"]))
        print(msg)

    def print_ordered_vars(self, vars):
        all_vars = [block[1] for block in self.ad_system.dof_manager.block_dof]
        print("Variables:")
        print(list(sorted(set(vars), key=lambda x: all_vars.index(x))))

    def print_matrix(self, print_dense: bool = False):
        print("---")
        print("Flash Variables:")
        self.print_ordered_vars(self.ph_subsystem["primary_vars"])
        for equ in self.ph_subsystem["equations"]:
            A, b = self.ad_system.assemble_subsystem(
                equ, self.ph_subsystem["primary_vars"]
            )
            print("---")
            print(equ)
            self.print_system(A, b, print_dense)
        print("---")

    def print_system(self, A, b, print_dense: bool = False):
        print("---")
        print("||Res||: ", np.linalg.norm(b))
        print("Cond: ", np.linalg.cond(A.todense()))
        # print("Eigvals: ", np.linalg.eigvals(A.todense()))
        print("Rank: ", np.linalg.matrix_rank(A.todense()))
        print("---")
        if print_dense:
            print("Residual:")
            print(b)
            print("Jacobian:")
            print(A.todense())
            print("---")

    def _history_entry(
        self,
        flash: str = "isenthalpic",
        method: str = "standard",
        iterations: int = 0,
        success: bool = False,
        variables: list[str] = list(),
        equations: list[str] = list(),
        **kwargs,
    ) -> None:
        """Makes an entry in the flash history."""

        self.flash_history.append(
            {
                "flash": flash,
                "method": method,
                "iterations": iterations,
                "success": success,
                "variables": str(variables),
                "equations": str(equations),
                "other": str(kwargs),
            }
        )
        if len(self.flash_history) > self._max_history:
            self.flash_history.pop(0)

    ### Flash methods -------------------------------------------------------------------------

    def isothermal_flash(
        self,
        copy_to_state: bool = False,
        initial_guess: Literal["iterate", "uniform"] | str = "iterate",
    ) -> bool:
        """Isothermal flash procedure to determine the composition based on given
        temperature of the mixture, pressure and feed fraction per component.

        Parameters:
            copy_to_state (bool): Copies the values to the STATE of the AD variables,
                additionally to ITERATE.
            initial_guess (optional): strategy for choosing the initial guess:

                - ``iterate``: values from ITERATE or STATE, if ITERATE not existent,
                - ``uniform``: uniform fractions adding up to 1 are used as initial guesses

                Can be extended by child classes.

        Returns:
            indicator if flash was successful or not. If not successful, the ITERATE will
            **not** be copied to the STATE, even if flagged ``True`` by ``copy_to_state``.

        """
        success = self._Newton_min(self.pT_subsystem, copy_to_state, initial_guess)
        return success

    def isenthalpic_flash(
        self,
        copy_to_state: bool = False,
        initial_guess: Literal["iterate", "uniform"] | str = "iterate",
    ) -> bool:
        """Isenthalpic flash procedure to determine the composition based on given
        specific enthalpy of the mixture, pressure and feed fractions per component.

        Parameters:
            copy_to_state (bool): Copies the values to the STATE of the AD variable,
                additionally to ITERATE.
            initial_guess (optional): strategy for choosing the initial guess:

                - ``iterate``: values from ITERATE or STATE, if ITERATE not existent,
                - ``uniform``: uniform fractions adding up to 1 are used as initial guesses

                Can be extended by child classes.

        Returns:
            indicator if flash was successful or not. If not successful, the ITERATE will
            **not** be copied to the STATE, even if flagged ``True`` by ``copy_to_state``.

        """
        success = self._Newton_min(self.ph_subsystem, copy_to_state, initial_guess)
        return success

    def evaluate_saturations(self, copy_to_state: bool = True) -> None:
        """Based on the thermodynamic state of the mixture, evaluates the volumetric phase
        fractions (saturations) based on the number of present phases.

        To be used after any flash procedure.

        If no phases are present (e.g. before any flash procedure), this method does nothing.

        Parameters:
            copy_to_state (optional): Copies the values to the STATE of the AD variable,
                additionally to ITERATE. Defaults to True.

        """
        if self.num_phases == 1:
            self._single_phase_saturation_evaluation(copy_to_state)
        if self.num_phases == 2:
            self._2phase_saturation_evaluation(copy_to_state)
        elif self.num_phases >= 3:
            self._multi_phase_saturation_evaluation(copy_to_state)

    def evaluate_specific_enthalpy(self, copy_to_state: bool = True) -> None:
        """Based on current pressure, temperature and phase fractions, evaluates the
        specific molar enthalpy.

        To be used after an isothermal flash.

        Use with care, if the equilibrium problem is coupled with e.g., the flow.

        Parameters:
            copy_to_state (optional): Copies the values to the STATE of the AD variable,
                additionally to ITERATE. Defaults to True.

        """
        # obtain values by forward evaluation
        equ_ = list()
        for phase in self.phases:
            equ_.append(phase.fraction * phase.specific_enthalpy(self._p, self._T))
        equ = sum(equ_)

        # if no phase present (list empty) zero is returned and enthalpy is zero
        if equ == 0:
            h = np.zeros(self.ad_system.dof_manager.mdg.num_subdomain_cells())
        # else evaluate this operator
        elif isinstance(equ, pp.ad.Operator):
            h = equ.evaluate(self.ad_system.dof_manager).val
        else:
            raise RuntimeError("Something went terribly wrong.")
        # write values in local var form
        self.ad_system.set_var_values(self.h_name, h, copy_to_state)

    def post_process_fractions(self, copy_to_state: bool = True) -> None:
        """Re-normalizes phase compositions and removes numerical artifacts
        (values bound between 0 and 1), and evaluates the reference phase fraction.

        Phase compositions (fractions of components in that phase) are nonphysical if a
        phase is not present. The unified flash procedure yields nevertheless values, possibly
        violating the unity constraint. Respective fractions have to be re-normalized in a
        post-processing step and set as regular phase composition.

        Also, removes artifacts outside the bound 0 and 1 for all molar fractions
        except feed fraction, which is **not** changed by the flash at all
        (the amount of matter is not supposed to change).

        Parameters:
            copy_to_state (optional): If True, copies the values to the STATE of the
                AD variable, additionally to ITERATE.

        """

        # evaluate reference phase fractions
        y_R = self._y_R.evaluate(self.ad_system.dof_manager).val
        self.ad_system.set_var_values(
            self.reference_phase.fraction_name, y_R, copy_to_state
        )

        for phase_e in self.phases:
            # remove numerical artifacts on phase fractions y
            y_e = self.ad_system.get_var_values(phase_e.fraction_name)
            y_e[y_e < 0.0] = 0.0
            y_e[y_e > 1.0] = 1.0
            self.ad_system.set_var_values(phase_e.fraction_name, y_e, copy_to_state)

            # extracting phase composition xi of phase e
            xi_e = list()
            for comp_c in phase_e:
                xi_ce = self.ad_system.get_var_values(
                    phase_e.fraction_of_component_name(comp_c)
                )
                xi_e.append(xi_ce)
            sum_xi_e = sum(xi_e)

            # re-normalize phase compositions and set regular phase composition chi of phase e
            for c, comp_c in enumerate(phase_e):
                xi_ce = xi_e[c]
                chi_ce = xi_ce / sum_xi_e
                # remove numerical artifacts
                xi_ce[xi_ce < 0.0] = 0.0
                xi_ce[xi_ce > 1.0] = 1.0
                chi_ce[chi_ce < 0.0] = 0.0
                chi_ce[chi_ce > 1.0] = 1.0
                # write values
                self.ad_system.set_var_values(
                    phase_e.fraction_of_component_name(comp_c),
                    xi_ce,
                    copy_to_state,
                )
                self.ad_system.set_var_values(
                    phase_e.normalized_fraction_of_component_name(comp_c),
                    chi_ce,
                    copy_to_state,
                )

    ### Model equations -----------------------------------------------------------------------

    def get_mass_conservation_for(
        self,
        component: Component,
        eliminate_ref_phase: bool = True,
    ) -> pp.ad.Operator:
        """Returns an equation representing the definition of the overall component fraction
        (mass conservation) for a component component.

            `` y_R = 1 - sum_{e != R} y_e``
            ``z_c - sum_e y_e * chi_ce = 0``
            ``z_c - chi_cR - sum_{e != R} y_e * (chi_ce - chi_cR) = 0``

        Parameters:
            component: a component in this composition
            eliminate_ref_phase (optional): If True, the reference phase molar fraction is
                eliminated by unity.

        Returns:
            AD operator representing the left-hand side of the equation (rhs=0).

        """
        # z_c
        equation = component.fraction
        if eliminate_ref_phase:
            chi_cR = self.reference_phase.fraction_of_component(component)
            # z_c  - chi_cR
            equation -= chi_cR
            # - sum_{e != R} y_e * (chi_ce - chi_cR)
            for phase in self.phases:
                if phase != self.reference_phase:
                    chi_ce = phase.fraction_of_component(component)
                    equation -= phase.fraction * (chi_ce - chi_cR)
        else:
            for phase in self.phases:
                chi_ce = phase.fraction_of_component(component)
                equation -= phase.fraction * chi_ce

        return equation

    def get_phase_fraction_unity(self) -> pp.ad.Operator:
        """Returns an equation representing the phase fraction unity

            ``1 - sum_e y_e = 0``

        Returns:
            AD operator representing the left-hand side of the equation (rhs=0).

        """
        equation = pp.ad.Scalar(1.0)

        for phase in self.phases:
            equation -= phase.fraction

        return equation

    def get_phase_saturation_unity(self) -> pp.ad.Operator:
        """Returns an equation representing the phase fraction unity

            ``1 - sum_e s_e = 0``

        Returns:
            AD operator representing the left-hand side of the equation (rhs=0).

        """
        equation = pp.ad.Scalar(1.0)

        for phase in self.phases:
            equation -= phase.saturation

        return equation

    def get_reference_phase_fraction_by_unity(self) -> pp.ad.Operator:
        """Returns an equation which expresses the fraction of the reference phase through
        the fractions of other phases by unity.

            ``y_R = 1 - sum_{e != R} y_e

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
        """Returns an equation which expresses the saturation of the reference phase through
        the saturations of other phases by unity.

            ``s_R = 1 - sum_{e != R} s_e

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
        """Returns a nonlinear equation representing the relation between the molar fraction
        of a phase and its volumetric fraction (saturation).

        The equation includes the unity of saturations, i.e.

            ``y_e = (rho_e * s_e) / (sum_f rho_f s_f)``
            ``y_e * rho - s_e * rho_e = 0``

        Parameters:
            phase: a phase in this composition
            eliminate_ref_phase (optional): If True, the reference phase saturation and
                fraction are eliminated by unity.

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
        """Returns an equation representing the specific molar enthalpy of the composition,
        based on it's definition:

            ``y_R = 1 - sum_{e != R} y_e``
            ``h - sum_e y_e * h_e(p,T) = 0``
            ``h - h_R - sum_{e != R} y_e * (h_e - h_R) = 0``

        Can be used to for the p-h flash as enthalpy constraint (T is an additional variable).

        Parameters:
            eliminate_ref_phase (optional): If True, the reference phase fraction is eliminated
                by unity.

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
        """Returns an equation representing the unity if the composition for a given phase e:

         ``1 - sum_c chi_ce = 0``

        Parameters:
            phase: a phase in this composition

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
        """Returns the two complementary equations for a phase e:

            ``min{y_e, 1 - sum_c chi_ce} = 0``

        Parameters:
            phase: a phase in this composition

        Returns:
            tuple of AD operators representing the left-hand side of the equation (rhs=0).

        """
        return (phase.fraction, self.get_composition_unity_for(phase))

    @abc.abstractmethod
    def get_equilibrium_equation(self, component: Component) -> pp.ad.Operator:
        """Abstract method to create a equilibrium equation for a component of the form

            ``f(x) = 0``

        Note:
            Equilibrium equations must be formulated with respect to the reference phase.

        Parameters:
            component: a component in this composition

        Returns:
            AD operator representing the left-hand side of the equation (rhs=0).

        """
        pass

    ### Flash methods -------------------------------------------------------------------------

    def _Newton_min(
        self,
        subsystem: dict,
        copy_to_state: bool,
        initial_guess: str,
    ) -> bool:
        """Performs a semi-smooth newton (Newton-min), where the complementary conditions are
        the semi-smooth part.

        Note:
            This looks exactly like a regular Newton since the semi-smooth part, especially the
            assembly of the sub-gradient, are wrapped in a special AD operator.

        Parameters:
            subsystem: specially structured dict containing equation and variable names.
            copy_to_state: flag to save the result as STATE, additionally to ITERATE.
            initial_guess: initial guess strategy

        Returns:
            a bool indicating the success of the method.

        """
        success = False
        var_names = subsystem["primary_vars"]
        equations = subsystem["equations"]

        self._set_initial_guess(initial_guess)

        # assemble linear system of eq for semi-smooth subsystem
        A, b = self.ad_system.assemble_subsystem(equations, var_names)

        # if residual is already small enough
        if np.linalg.norm(b) <= self.flash_tolerance:
            success = True
            iter_final = 0
        else:
            # this changes dependent on flash type but also if other models accessed the system
            prolongation = self.ad_system.dof_manager.projection_to(
                var_names
            ).transpose()

            for i in range(self.max_iter_flash):

                # solve iteration and add to ITERATE state additively
                dx = sps.linalg.spsolve(A, b)
                DX = prolongation * dx
                self.ad_system.dof_manager.distribute_variable(
                    DX,
                    variables=var_names,
                    additive=True,
                    to_iterate=True,
                )
                # counting necessary number of iterations
                iter_final = i + 1  # shift since range() starts with zero
                A, b = self.ad_system.assemble_subsystem(equations, var_names)

                # in case of convergence
                if np.linalg.norm(b) <= self.flash_tolerance:

                    # setting STATE to newly found solution
                    if copy_to_state:
                        X = self.ad_system.dof_manager.assemble_variable(
                            variables=var_names, from_iterate=True
                        )
                        self.ad_system.dof_manager.distribute_variable(
                            X, variables=var_names
                        )

                    success = True
                    break

        # append history entry
        self._history_entry(
            flash="isenthalpic" if self.T_name in var_names else "isothermal",
            method="Newton-min",
            iterations=iter_final,
            success=success,
            variables=var_names,
            equations=equations,
        )

        return success

    def _set_initial_guess(self, initial_guess: str) -> None:
        """Auxillary function to set the initial values for phase fractions, phase compositions
        and temperature, based on the chosen strategy.
        """

        if initial_guess == "iterate":
            pass  # DofManager does this by default
        elif initial_guess == "uniform":
            nc = self.ad_system.dof_manager.mdg.num_subdomain_cells()
            # uniform values for phase fraction
            val_phases = 1.0 / self.num_phases
            for phase in self.phases:
                self.ad_system.set_var_values(
                    phase.fraction_name, val_phases * np.ones(nc)
                )
                # uniform values for composition of this phase
                val = 1.0 / phase.num_components
                for component in self.components:
                    self.ad_system.set_var_values(
                        phase.fraction_of_component_name(component),
                        val * np.ones(nc),
                    )

    ### Saturation post-processing ------------------------------------------------------------

    def _single_phase_saturation_evaluation(self, copy_to_state: bool = True) -> None:
        """If only one phase is present, we assume it occupies the whole pore space."""
        phase = self.reference_phase
        values = np.ones(self.ad_system.dof_manager.mdg.num_subdomain_cells())
        self.ad_system.set_var_values(phase.saturation_name, values, copy_to_state)

    def _2phase_saturation_evaluation(self, copy_to_state: bool = True) -> None:
        """Calculates the saturation value assuming phase molar fractions are given.
        In the case of 2 phases, the evaluation is straight forward.

        It holds:
            s_i = 1 / (1 + y_j / (1 - y_j) * rho_i / rho_j) , i != j

        """
        # get reference to phases
        phase1, phase2 = (phase for phase in self.phases)
        # shortening the name space
        dm = self.ad_system.dof_manager
        # get phase molar fraction values
        y1 = self.ad_system.get_var_values(phase1.fraction_name)
        y2 = self.ad_system.get_var_values(phase2.fraction_name)

        # get density values for given pressure and enthalpy
        rho1 = phase1.density(self.p, self.T).evaluate(dm)
        if isinstance(rho1, pp.ad.Ad_array):
            rho1 = rho1.val
        rho2 = phase2.density(self._p, self._T).evaluate(dm)
        if isinstance(rho2, pp.ad.Ad_array):
            rho2 = rho2.val

        # allocate saturations, size must be the same
        s1 = np.zeros(y1.size)
        s2 = np.zeros(y1.size)

        # TODO test sensitivity of this
        phase1_saturated = y1 == 1.0  # equal to phase2_vanished
        phase2_saturated = y2 == 1.0  # equal to phase1_vanished

        # calculate only non-saturated cells to avoid division by zero
        # set saturated or "vanishing" cells explicitly to 1., or 0. respectively
        idx = np.logical_not(phase2_saturated)
        y2_idx = y2[idx]
        rho1_idx = rho1[idx]
        rho2_idx = rho2[idx]
        s1[idx] = 1.0 / (1.0 + y2_idx / (1.0 - y2_idx) * rho1_idx / rho2_idx)
        s1[phase1_saturated] = 1.0
        s1[phase2_saturated] = 0.0

        idx = np.logical_not(phase1_saturated)
        y1_idx = y1[idx]
        rho1_idx = rho1[idx]
        rho2_idx = rho2[idx]
        s2[idx] = 1.0 / (1.0 + y1_idx / (1.0 - y1_idx) * rho2_idx / rho1_idx)
        s2[phase1_saturated] = 0.0
        s2[phase2_saturated] = 1.0

        # write values to AD system
        self.ad_system.set_var_values(phase1.saturation_name, s1, copy_to_state)
        self.ad_system.set_var_values(phase2.saturation_name, s2, copy_to_state)

    def _multi_phase_saturation_evaluation(self, copy_to_state: bool = True) -> None:
        """Calculates the saturation value assuming phase molar fractions are given.
        Valid for compositions with at least 3 phases.
        In this case a linear system has to be solved for each multiphase cell

        It holds for all i = 1... m, where m is the number of phases:
            1 = sum_{j != i} (1 + rho_j / rho_i * chi_i / (1 - chi_i)) s_j
        """
        # shortening name space
        dm = self.ad_system.dof_manager
        nc = dm.mdg.num_subdomain_cells()
        # molar fractions per phase
        y = [
            self.ad_system.get_var_values(phase.saturation_name)
            for phase in self.phases
        ]
        # densities per phase
        rho = list()
        for phase in self.phases:
            rho_e = phase.density(self.p, self.T).evaluate(dm)
            if isinstance(rho_e, pp.ad.Ad_array):
                rho_e = rho_e.val
            rho.append(rho_e)

        mat_per_eq = list()

        # list of indicators per phase, where the phase is fully saturated
        saturated = list()
        # where one phase is saturated, the other vanish
        vanished = [np.zeros(nc, dtype=bool) for _ in self.phases]

        for i in range(self.num_phases):
            # get the DOFS where one phase is fully saturated
            # TODO check sensitivity of this
            saturated_i = y[i] == 1.0
            saturated.append(saturated_i)

            # store information that other phases vanish at these DOFs
            for j in range(self.num_phases):
                if j == i:
                    # a phase can not vanish and be saturated at the same time
                    continue
                else:
                    # where phase i is saturated, phase j vanishes
                    # Use OR to accumulate the bools per i-loop without overwriting
                    vanished[j] = np.logical_or(vanished[j], saturated_i)

        # indicator which DOFs are saturated for the vector of stacked saturations
        saturated = np.hstack(saturated)
        # indicator which DOFs vanish
        vanished = np.hstack(vanished)
        # all other DOFs are in multiphase regions
        multiphase = np.logical_not(np.logical_or(saturated, vanished))

        # construct the matrix for saturation flash
        # first loop, per block row (equation per phase)
        for i in range(self.num_phases):
            mats = list()
            # second loop, per block column (block per phase per equation)
            for j in range(self.num_phases):
                # diagonal values are zero
                # This matrix is just a placeholder
                if i == j:
                    mats.append(sps.diags([np.zeros(nc)]))
                # diagonals of blocks which are not on the main diagonal, are non-zero
                else:
                    denominator = 1 - y[i]
                    # to avoid a division by zero error, we set it to one
                    # this is arbitrary, but respective matrix entries will be sliced out
                    # since they correspond to cells where one phase is saturated,
                    # i.e. the respective saturation is 1., the other 0.
                    denominator[denominator == 0.0] = 1.0
                    d = 1.0 + rho[j] / rho[i] * y[i] / denominator

                    mats.append(sps.diags([d]))

            # rectangular matrix per equation
            mat_per_eq.append(np.hstack(mats))

        # Stack matrices per equation on each other
        # This matrix corresponds to the vector of stacked saturations per phase
        mat = np.vstack(mat_per_eq)
        # TODO permute DOFS to get a block diagonal matrix. This one has a large band width
        mat = sps.csr_matrix(mat)

        # projection matrix to DOFs in multiphase region
        # start with identity in CSR format
        projection = sps.diags([np.ones(len(multiphase))]).tocsr()
        # slice image of canonical projection out of identity
        projection = projection[multiphase]
        projection_transposed = projection.transpose()

        # get sliced system
        rhs = projection * np.ones(nc * self.num_phases)
        mat = projection * mat * projection_transposed

        s = sps.linalg.spsolve(mat.tocsr(), rhs)

        # prolongate the values from the multiphase region to global DOFs
        saturations = projection_transposed * s
        # set values where phases are saturated or have vanished
        saturations[saturated] = 1.0
        saturations[vanished] = 0.0

        # distribute results to the saturation variables
        for i, phase in enumerate(self.phases):
            vals = saturations[i * nc : (i + 1) * nc]
            self.ad_system.set_var_values(phase.saturation_name, vals, copy_to_state)

    ### Special methods -----------------------------------------------------------------------

    def __str__(self) -> str:
        """Returns string representation of the composition,
        with information about present components.

        """
        out = f"Composition with {self.num_components} components:"
        for component in self.components:
            out += f"\n{component.name}"
        out += f"\nand {self.num_phases} phases:"
        for phase in self.phases:
            out += f"\n{phase.name}"
        return out
