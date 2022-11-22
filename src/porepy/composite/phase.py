""" Contains the private base class for all phases.

This module is not imported by default into the composite subpackage,
since the user is not supposed to be able to create phase classes, only the composition class.

"""
from __future__ import annotations

import abc
from typing import Generator, Union

import numpy as np

import porepy as pp

from ._composite_utils import VARIABLE_SYMBOLS, CompositionalSingleton
from .component import Component

VarLike = Union[pp.ad.MergedVariable, pp.ad.Variable, pp.ad.Operator, float, int]
"""Union type representing variable input for thermodynamic properties."""


class Phase(abc.ABC, metaclass=CompositionalSingleton):
    """Abstract base class for phases in a multiphase multicomponent mixture.

    The term 'phase' includes both, states of matter and general fields.
    A phase is identified by the (time-dependent) region/volume it occupies and a
    respective velocity field (or flux) in that region.

    Phases have physical properties, dependent on the thermodynamic state and the composition.
    The composition variables (molar fractions of present components)
    can be accessed by internal reference.

    Warning:
        This class is only meant to be instantiated by a Composition,
        since the number of phases is an unknown in the thermodynamic equilibrium problem.

    The Phase is a Singleton per AD system, using the **given** name as a unique identifier.
    A Phase class with name ``X`` can only be present once in a system.
    Ambiguities must be avoided due to central storage of the fractional values in the
    grid data dictionaries.

    Parameters:
        ad_system: AD system in which this phase is present cell-wise in each subdomain.
        name (optional): given name for this phase.
            Used as an unique identifier for singletons.

    """

    def __init__(self, ad_system: pp.ad.ADSystem, name: str = "") -> None:
        super().__init__()

        ### PUBLIC

        self.ad_system: pp.ad.ADSystem = ad_system
        """The AD system passed at instantiation."""

        #### PRIVATE
        self._name = name
        self._present_components: list[Component] = list()

        # Instantiate saturation and molar phase fraction (secondary variables)
        self._s: pp.ad.MergedVariable = ad_system.create_variable(self.saturation_name)
        self._fraction: pp.ad.MergedVariable = ad_system.create_variable(
            self.fraction_name
        )

        nc = ad_system.dof_manager.mdg.num_subdomain_cells()
        ad_system.set_var_values(self.saturation_name, np.zeros(nc), True)
        ad_system.set_var_values(self.fraction_name, np.zeros(nc), True)

        # contains extended fractional values per present component name (key)
        self._ext_composition: dict[Component, pp.ad.MergedVariable] = dict()
        # contains regular fractional values per present component name (key)
        self._composition: dict[Component, pp.ad.MergedVariable] = dict()

    def __iter__(self) -> Generator[Component, None, None]:
        """Generator over components present in this phase.

        Notes:
            The order from this iterator is used for choosing e.g. the values in a
            list of 'numpy.array' when setting initial values.
            Use the order returned here every time you deal with component-related values
            for components in this phase.

        Yields:
            components present in this phase.
        """
        for substance in self._present_components:
            yield substance

    @property
    def name(self) -> str:
        """Name of this phase given at instantiation."""
        return self._name

    @property
    def num_components(self) -> int:
        """Number of present (added) components."""
        return len(self._present_components)

    @property
    def saturation_name(self) -> str:
        """Name for the saturation variable, given by the general symbol and :meth:`name`."""
        return f"{VARIABLE_SYMBOLS['phase_saturation']}_{self.name}"

    @property
    def saturation(self) -> pp.ad.MergedVariable:
        """
        | Math. Dimension:        scalar
        | Phys. Dimension:        [-] fractional

        Returns:
            saturation (volumetric fraction), a secondary variable on the whole domain.
            Indicates how much of the (local) volume is occupied by this phase (per cell).
            It is supposed to represent the value at thermodynamic equilibrium.

        """
        return self._s

    @property
    def fraction_name(self) -> str:
        """Name for the molar fraction variable, given by the general symbol and phase name."""
        return f"{VARIABLE_SYMBOLS['phase_fraction']}_{self.name}"

    @property
    def fraction(self) -> pp.ad.MergedVariable:
        """
        | Math. Dimension:        scalar
        | Phys. Dimension:        [-] fractional

        Returns:
            molar phase fraction, a secondary variable on the whole domain.
            Indicates how many of the total moles belong to this phase (per cell).
            It is supposed to represent the value at thermodynamic equilibrium.

        """
        return self._fraction

    def fraction_of_component_name(self, component: Component) -> str:
        """
        Parameters:
            component: component for which the respective name is requested.

        Returns:
            name of the respective variable, given by the general symbol, the
            component name and the phase name.

        """
        return f"{VARIABLE_SYMBOLS['phase_composition']}_{component.name}_{self.name}"

    def fraction_of_component(
        self, component: Component
    ) -> pp.ad.MergedVariable | pp.ad.Scalar:
        """
        | Math. Dimension:        scalar
        | Phys. Dimension:        [-] fractional

        If a phase is present (phase fraction is strictly positive), the extended component
        fraction (this one) coincides with the regular component fraction.
        If a phase vanishes (phase fraction is zero), the extended fractions represent
        non-physical values at equilibrium.
        The extended phase composition does not fulfill unity.
        In the case of a vanished phase, the regular phase composition is obtained by
        re-normalizing the extended phase composition, such that unity is fulfilled.

        Parameters:
            component: a component present in this phase

        Returns:
            extended molar fraction of a component in this phase,
            a secondary variable on the whole domain (cell-wise).
            Indicates how many of the moles in this phase belong to the component.
            It is supposed to represent the value at thermodynamic equilibrium.

            Returns always zero if a component is not modelled (added) to this phase.

        """
        if component in self._ext_composition:
            return self._ext_composition[component]
        else:
            return pp.ad.Scalar(0.0)

    def normalized_fraction_of_component_name(self, component: Component) -> str:
        """
        Parameters:
            component: component for which the respective name is requested.

        Returns:
            name of the respective variable, given by the general symbol, the
            component name and the phase name.

        """
        return (
            f"{VARIABLE_SYMBOLS['normalized_phase_composition']}_"
            f"{component.name}_{self.name}"
        )

    def normalized_fraction_of_component(
        self, component: Component
    ) -> pp.ad.MergedVariable | pp.ad.Scalar:
        """
        | Math. Dimension:        scalar
        | Phys. Dimension:        [-] fractional

        If a phase is present (phase fraction is strictly positive), the regular component
        fraction (this one) coincides with the extended component fraction.
        If a phase vanishes (phase fraction is zero), the regular component fraction
        is obtained by re-normalizing the extended component fraction.

        Parameters:
            component: a component present in this phase

        Returns:
            extended molar fraction of a component in this phase,
            a secondary variable on the whole domain (cell-wise).
            Indicates how many of the moles in this phase belong to the component.
            It is supposed to represent the value at thermodynamic equilibrium.

            Returns always zero if a component is not modelled (added) to this phase.

        """
        if component in self._ext_composition:
            return self._composition[component]
        else:
            return pp.ad.Scalar(0.0)

    def add_component(
        self,
        component: Component | list[Component],
    ) -> None:
        """Adds components which are expected by the modeler in this phase.

        If a component was already added, nothing happens. Components appear uniquely in a
        phase.

        This design choice enables the association 'component in phase', as well as proper
        storage of related, fractional variables.

        Parameters:
            a component, or list of components, which are expected in this phase.

        Raises:
            RuntimeError: if the component was instantiated using a different AD system than
            the one used for the phase.

        """
        if isinstance(component, Component):
            component = [component]  # type: ignore
        present_components = [ps.name for ps in self._present_components]

        for comp in component:
            # sanity check when using the AD framework
            if self.ad_system != comp.ad_system:
                raise RuntimeError(
                    f"Component '{comp.name}' instantiated with a different AD system."
                )
            # skip already present components:
            if comp.name in present_components:
                continue

            # create compositional variables for the component in this phase
            ext_fraction_name = self.fraction_of_component_name(comp)
            fraction_name = self.normalized_fraction_of_component_name(comp)
            ext_comp_fraction = self.ad_system.create_variable(ext_fraction_name)
            comp_fraction = self.ad_system.create_variable(fraction_name)

            # set fractional values to zero
            nc = self.ad_system.dof_manager.mdg.num_subdomain_cells()
            self.ad_system.set_var_values(ext_fraction_name, np.zeros(nc), True)
            self.ad_system.set_var_values(fraction_name, np.zeros(nc), True)

            # store reference to present substance
            self._present_components.append(comp)
            # store the compositional variable
            self._ext_composition.update({comp: ext_comp_fraction})
            self._composition.update({comp: comp_fraction})

    ### Physical properties -------------------------------------------------------------------

    def mass_density(self, p: VarLike, T: VarLike) -> VarLike:
        """Uses the  molar mass in combination with the molar masses and fractions
        of components in this phase, to compute the mass density of the phase.

        | Math. Dimension:        scalar
        | Phys. Dimension:        [kg / REV]

        Parameters:
            p: pressure
            T: temperature

        Returns: mass density of this phase.

        """
        weight = 0.0
        # add the mass-weighted fraction for each present substance.
        # if no components are present, the weight is zero!
        for component in self._present_components:
            weight += component.molar_mass() * self._composition[component]

        # Multiply the mass weight with the molar density and return the operator
        return weight * self.density(p, T)

    @abc.abstractmethod
    def density(self, p: VarLike, T: VarLike) -> VarLike:
        """
        | Math. Dimension:        scalar
        | Phys. Dimension:        [mol / REV]

        Parameters:
            p: pressure
            T: temperature

        Returns: mass density of this phase.

        """
        pass

    @abc.abstractmethod
    def specific_enthalpy(self, p: VarLike, T: VarLike) -> VarLike:
        """
        | Math. Dimension:        scalar
        | Phys.Dimension:         [kJ / mol / K]

        Parameters:
            p: pressure
            T: temperature

        Returns: specific molar enthalpy of this phase.

        """
        pass

    @abc.abstractmethod
    def dynamic_viscosity(self, p: VarLike, T: VarLike) -> VarLike:
        """
        | Math. Dimension:        scalar
        | Phys. Dimension:        [mol / m / s]

        Parameters:
            p: pressure
            T: temperature

        Returns: dynamic viscosity of this phase.

        """
        pass

    @abc.abstractmethod
    def thermal_conductivity(self, p: VarLike, T: VarLike) -> VarLike:
        """
        | Math. Dimension:    2nd-order tensor
        | Phys. Dimension:    [W / m / K]

        Parameters:
            p: pressure
            T: temperature

        Returns: thermal conductivity of this phase.

        """
        pass
