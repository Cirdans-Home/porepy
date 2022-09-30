"""Contains concrete implementation of phases."""
from __future__ import annotations

import porepy as pp
import numpy as np

from .phase import Phase
from ._composite_utils import R_IDEAL, T_REF, P_REF, CP_REF, V_REF, U_REF

__all__ = ["IncompressibleFluid", "IdealGas"]

# TODO ADify properly
class IncompressibleFluid(Phase):
    """Ideal, Incompressible fluid with constant density of 1,000,000 moles per V_REF.
    
    The EOS is reduced to
    
    const rho = 1000000 / V_REF ( = 1000000 / V )
    V = V_REF
    
    """

    def density(self, p, T):
        return pp.ad.Array(np.array([1000000. / V_REF]))

    def specific_enthalpy(self, p, T):
        return U_REF + P_REF / (1000000 / V_REF) + CP_REF * (T - T_REF) + V_REF * (p - P_REF)

    def dynamic_viscosity(self, p, T):
        return pp.ad.Scalar([1.])

    def thermal_conductivity(self, p, T):
        return pp.ad.Scalar([1.])


class IdealGas(Phase):
    """Ideal water vapor phase with EoS:
    
     rho = n / V  = p / (R * T)

    """

    def density(self, p, T):
        return p / (T * R_IDEAL)

    def specific_enthalpy(self, p, T):
        # enthalpy at reference state is
        # h = u + p / rho(p,T)
        # which due to the ideal gas law simplifies to
        # h = u + R * T
        return U_REF + R_IDEAL * T_REF + CP_REF * (T - T_REF)

    def dynamic_viscosity(self, p, T):
        return pp.ad.Scalar([1.])

    def thermal_conductivity(self, p, T):
        return pp.ad.Scalar([1.])
