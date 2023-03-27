""" Functionality to interface with Gmsh, mainly by translating information from a PorePy
format to that used by the Gmsh Python API. For examples on how that can be used, see
FractureNetwork2d and FractureNetwork3d.

Content:
    Tags: Enum with fixed numerical representation of geometric objects. Used for tagging
        geometric objects internally in PorePy.
    PhysicalNames: Enum with fixed string representation of geometric objects. The
        mesh generated by gmsh will use these to tag cells on geometric objcets.
    GmshData1d, 2d, 3d: dataclasses for the specification of geometries in the various
        dimensions.
    GmshWriter: Interface to Gmsh. Takes a GmshData*d object and translates it to a
        gmsh model. Can also mesh.

"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union

import gmsh
import numpy as np

__all__ = [
    "GmshData1d",
    "GmshData2d",
    "GmshData3d",
    "Tags",
    "PhysicalNames",
    "GmshWriter",
]


class Tags(Enum):
    """Numerical tags used to identify special objects in a mixed-dimensional
    geometry. These may be mapped to the string-based tag system used in Gmsh
    (see PhysicalNames)
    """

    # A neutral tag - objects with this tag will not be accounted for in any praticular
    # way in grid and geometry processing.
    NEUTRAL = 0

    # Objects used to define the domain boundary
    DOMAIN_BOUNDARY_POINT = 1
    DOMAIN_BOUNDARY_LINE = 2
    DOMAIN_BOUNDARY_SURFACE = 3

    # A fracture
    FRACTURE = 10

    # Auxiliary line or plane. Used for constraints.
    AUXILIARY_LINE = 11
    AUXILIARY_PLANE = 12

    # A fracture tip - can be point or line
    FRACTURE_TIP = 20

    # Intersection line between (at least) two fractures. Ambient dimension is 3.
    FRACTURE_INTERSECTION_LINE = 21

    # Line on both fracture and a domain boundary
    FRACTURE_BOUNDARY_LINE = 22

    # Point on the intersection between fractures.
    # Will at least two fractures in 2d, at least three fractures in 3d.
    FRACTURE_INTERSECTION_POINT = 30

    FRACTURE_CONSTRAINT_INTERSECTION_POINT = 31

    # Intersection point between fracture and domain boundary
    FRACTURE_BOUNDARY_POINT = 32


class PhysicalNames(Enum):
    """String-based tags used to assign physical names (Gmsh jargon) to classes
    of objects in a mixed-dimensional geometry.
    """

    # Note that neutral tags have no physical names - they should not receive special treatment

    # The assumption (for now) is that there is a single boundary
    # This may change if we implement DD - but then we need to revisit these concepts to
    # include (metis?) partitioning
    DOMAIN = "DOMAIN"

    # Fractures
    FRACTURE = "FRACTURE_"

    # Auxiliary line and plane
    AUXILIARY_LINE = "AUXILIARY_LINE_"
    AUXILIARY_PLANE = "AUXILIARY_PLANE_"

    # Objects used to define the domain boundary
    DOMAIN_BOUNDARY_POINT = "DOMAIN_BOUNDARY_POINT_"

    DOMAIN_BOUNDARY_LINE = "DOMAIN_BOUNDARY_LINE_"

    DOMAIN_BOUNDARY_SURFACE = "DOMAIN_BOUNDARY_SURFACE_"

    # A fracture tip - can be point or line
    FRACTURE_TIP = "FRACTURE_TIP_"

    # Line on both fracture and a domain boundary
    FRACTURE_BOUNDARY_LINE = "FRACTURE_BOUNDARY_LINE_"

    # Intersection line between (at least) two fractures. Ambient dimension is 3.
    FRACTURE_INTERSECTION_LINE = "FRACTURE_INTERSECTION_LINE_"

    # Point on the intersection between fractures.
    # Will at least two fractures in 2d, at least three fractures in 3d.
    FRACTURE_INTERSECTION_POINT = "FRACTURE_INTERSECTION_POINT_"

    # Intersection point between fracture and domain boundary
    FRACTURE_BOUNDARY_POINT = "FRACTURE_BOUNDARY_POINT_"

    # Point defined in the intersection between a fracture and constraints / auxiliary planes.
    # Ambient dimension is 3.
    FRACTURE_CONSTRAINT_INTERSECTION_POINT = "FRACTURE_CONSTRAINT_INTERSECTION_POINT_"


def _tag_to_physical_name(tag: Union[int, Tags]) -> str:
    # Convenience function to map from numerical to string representation of a geometric object
    #    if isinstance(tag, Tags):
    t = Tags(tag)
    #    else:
    #        t = Tags(tag)
    for pn in PhysicalNames:
        if pn.name == t.name:
            return pn.value

    raise KeyError(f"Found no physical name corresponding to tag {tag}")


@dataclass
class _GmshData:
    # Common information

    # Points used in the geometry specification
    pts: np.ndarray
    # Mesh size for each point
    mesh_size: np.ndarray
    # 1d lines needed in the description. The third row should contain tags for the
    # lines (note that these are needed for all lines, whereas the field physical lines
    # is only used for lines that should be represented in the output mesh file).
    lines: np.ndarray
    # Mapping from indices of points (referring to the ordering in points) to
    # numerical tags for the points.
    physical_points: dict[int, Tags]
    # Mapping from indices of lines (referring to the ordering in lines) to numerical tags
    # for the lines.
    physical_lines: dict[int, Tags]


@dataclass
class GmshData1d(_GmshData):
    # Dimesion is 1.
    # 1d data is not fully supported yet.
    dim: int = 1


@dataclass
class GmshData2d(_GmshData):

    dim: int = 2


@dataclass
class GmshData3d(_GmshData):

    # Polygons (both boundary surfaces, fractures and auxiliary lines).
    # See FractureNetwork3d._poly_2_segment() for examples of usage.
    polygons: tuple[list[np.ndarray], list[np.ndarray]]
    # Tags used to identify the type of polygons.
    polygon_tags: dict[int, Tags]
    # Physical name information for polygons. Used to set gmsh tags for objects to be
    # represented in hte output mesh.
    physical_surfaces: dict[int, Tags]
    # Lines to be embedded in surfaces. Outer list has one item per polygon, inner has
    # index of lines to embed.
    lines_in_surface: list[list[int]]

    dim: int = 3


class GmshWriter:
    """Interface to Gmsh's python API.

    The geometry specification (in the form of a GmshData object) is converted to a
    model (in the Gmsh sense) upon initiation of a GmshWriter. A mesh can then be
    constructed either by the method generate, or by directly working with the
    gmsh api in a client script.

    """

    # Class variable keeping track of whether gmsh has been initialized.
    # This does not account for calls to gmsh.initialize() and gmsh.finalize()
    # outside the class.
    gmsh_initialized = False

    def __init__(self, data: Union[GmshData2d, GmshData3d]) -> None:
        """Initialization feeds the geometry specification to Gmsh.

        Parameters:
            data: Geometry specification.
        """
        self._dim = data.dim
        self._data = data

        self.define_geometry()

    def set_gmsh_options(self, options: Optional[dict] = None) -> None:
        """Set Gmsh options. See Gmsh documentation for choices available.

        Parameters:
            options (dict): Options to set. Keys should be recognizable for Gmsh.

        """
        if options is None:
            options = {}

        for key, val in options:
            if isinstance(val, (int, float)):
                try:
                    gmsh.option.setNumber(key, val)
                except Exception:
                    raise ValueError(
                        f"Could not set numeric gmsh option with key {key}"
                    )
            elif isinstance(val, str):
                try:
                    gmsh.option.setString(key, val)
                except Exception:
                    raise ValueError(f"Could not set string gmsh option with key {key}")

    def define_geometry(self) -> None:
        """Feed the geometry specified in self._data to gmsh."""

        # Initialize gmsh if this is not done before
        if not GmshWriter.gmsh_initialized:
            gmsh.initialize()
            GmshWriter.gmsh_initialized = True

        gmsh.option.setNumber("General.Verbosity", 3)

        # All geometries need their points
        self._point_tags = self._add_points()

        # The boundary, and the domain, must be specified prior to the
        # co-dimension 1 objects (fractures) so that the latter can be
        # embedded in the domain. This requires some specialized treatment
        # in 2d and 3d, since the boundary and fractures are made of up
        # different geometric objects in the two:
        if self._dim == 1:
            # This should not be difficult, but has not been prioritized.
            raise NotImplementedError("1d geometries are not implemented")

        elif self._dim == 2:
            # First add the domain. This will add the lines that form the domain
            # boundary, but not the fracture lines.
            self._domain_tag = self._add_domain_2d()

            # Next add the fractures (and constraints). This adds additional lines,
            # and embeds them in the domain.
            self._add_fractures_2d()

        else:
            # In 3d, we can first add lines to the description, they are only used
            # to define surfaces later.

            # Make a index list covering all the lines
            inds = np.arange(self._data.lines.shape[1])

            # The function to write lines assumes that the lines are formed by
            # point indices (first two rows), tags (third) and line index (fourth)
            # Add the latter, essentially saying that each line is a separate line.
            if self._data.lines.shape[0] < 4:
                self._data.lines = np.vstack((self._data.lines, inds))

            # Now we can write the lines
            # The lines are not directly embedded in the domain.
            # NOTE: If we ever get around to do a 1d-3d model (say a well), this may
            # be a place to do that.
            self._line_tags = self._add_lines(inds, embed_in_domain=False)

            # Next add the domain. This adds the boundary surfaces, embeds lines in
            # the relevant boundary surfaces, and constructs a 3d volume from the
            # surfaces
            self._domain_tag = self._add_domain_3d()

            # Finally, we can add the fractures and constraints and embed them in
            # the domain
            self._add_fractures_3d()

        gmsh.model.geo.synchronize()

    def generate(
        self,
        file_name: str,
        ndim: int = -1,
        write_geo: bool = False,
        clear_gmsh: bool = True,
        finalize: bool = True,
    ) -> None:
        """Make gmsh generate a mesh and write it to specified mesh size.

        The mesh is generated from the geometry specified in gmsh.model.

        NOTE: We have experienced issues relating to memory leakages in gmsh which
        manifest when gmsh is initialized and finalized several times in a session.
        In these situation, best practice seems to be not to finalize gmsh after
        mesh generation (set finalize=False, but rather clear the gmsh model by setting
        clear_gmsh=True), and finalize gmsh from the outside.

        Parameters:
            file_name (str): Name of the file. The suffix '.msh' is added if necessary.
            ndim (int, optional): Dimension of the grid to be made. If not specified, the
                dimension of the data used to initialize this GmshWriter will be used.
            write_geo (bool, optional): If True, the geometry will be written before meshing.
                The name of the file will be file_name.geo_unrolled. Defaults to False.
            clear_gmsh (bool, optional): If True, the function gmsh.clear() is called after
                mesh generation. This will delete the geometry from gmsh.
            finalize (bool, optional): If True (default), the finalize method of the gmsh
                module is called after mesh generation. If set to False, gmsh should be
                finalized by a direct call to gmsh.finalize(); note however that if this
                is done, Gmsh cannot be accessed either from the outside or by an
                instance of the GmshWriter class before gmsh.initialize() is called.

        """
        if ndim == -1:
            ndim = self._dim
        if file_name[-4:] != ".msh":
            file_name = file_name + ".msh"
        if write_geo:
            fn = file_name[:-4] + ".geo_unrolled"
            gmsh.write(fn)

        for dim in range(1, ndim + 1):
            try:
                gmsh.model.mesh.generate(dim=dim)
            except Exception as exc:
                s = f"Gmsh meshing failed in dimension {dim}. Error message:\n"
                s += str(exc)
                print(s)
                raise exc
        gmsh.write(file_name)

        if clear_gmsh:
            gmsh.clear()
        if finalize:
            gmsh.finalize()
            GmshWriter.gmsh_initialized = False

    def _add_points(self) -> list[int]:
        """Add points to gmsh. Also set physical names for points if required."""
        point_tags = []
        for pi, sz in enumerate(self._data.mesh_size):

            if self._dim == 2:
                x, y = self._data.pts[:, pi]
                z = 0
            else:
                x, y, z = self._data.pts[:, pi]
            point_tags.append(gmsh.model.geo.addPoint(x, y, z, sz))

            if pi in self._data.physical_points:
                point_type = self._data.physical_points[pi]
                phys_name = _tag_to_physical_name(point_type) + str(pi)
                gmsh.model.geo.synchronize()
                ps = gmsh.model.addPhysicalGroup(0, [point_tags[-1]])
                gmsh.model.setPhysicalName(0, ps, phys_name)
        return point_tags

    # Write fractures

    def _add_fractures_1d(self):
        raise NotImplementedError("1d domains are not supported yet")

    def _add_fractures_2d(self) -> None:
        """Add all fracture lines to gmsh."""
        # Only add the lines that correspond to fractures or auxiliary lines.
        # Boundary lines are added elsewhere.
        # NOTE: Here we operate on the numerical tagging of lines (the attribute edges
        # in FractureNetwork2d), thus we must compare with the values of the Tags.
        inds = np.argwhere(
            np.logical_or.reduce(
                (
                    self._data.lines[2] == Tags.FRACTURE.value,
                    self._data.lines[2] == Tags.AUXILIARY_LINE.value,
                )
            )
        ).ravel()
        self._frac_tags = self._add_lines(inds, embed_in_domain=True)

    def _add_fractures_3d(self) -> None:
        """Add fracture polygons to gmsh"""

        if not isinstance(self._data, GmshData3d):
            raise ValueError("Need 3d geometry to write fractures")

        # Loop over the polygons, find those tagged as fractures or auxiliary planes
        # (which could be constraints in the meshing). Add these to the information
        # to be written.
        indices_to_write = []
        for ind, tag in self._data.polygon_tags.items():
            if tag in (Tags.FRACTURE, Tags.AUXILIARY_PLANE):
                indices_to_write.append(ind)

        self._frac_tags = self._add_polygons_3d(indices_to_write, embed_in_domain=True)

    def _add_polygons_3d(self, inds: list, embed_in_domain: bool) -> list[int]:
        """Generic method to add polygons to a 3d geometry"""

        if not isinstance(self._data, GmshData3d):
            raise ValueError("Need 3d geometry to write polygons")

        if self._data.physical_surfaces is not None:
            phys_surf = self._data.physical_surfaces
        else:
            phys_surf = {}

        gmsh.model.geo.synchronize()

        line_dim = 1
        surf_dim = 2

        surf_tag = []

        to_phys_tags: list[tuple[int, str, list[int]]] = []

        for pi in inds:
            line_tags = []
            for line in self._data.polygons[0][pi]:
                line_tags.append(self._line_tags[line])

            loop_tag = gmsh.model.geo.addCurveLoop(line_tags, reorient=True)
            surf_tag.append(gmsh.model.geo.addPlaneSurface([loop_tag]))

            # Register the surface as physical if relevant. This will make gmsh export
            # the cells on the surface.
            if pi in phys_surf:
                physical_name = _tag_to_physical_name(phys_surf[pi])
                to_phys_tags.append((pi, physical_name + str(pi), [surf_tag[-1]]))

        # Update the model with all added surfaces
        gmsh.model.geo.synchronize()

        # Add the surfaces as physical tags if so specified.
        for (pi, phys_name, tag) in to_phys_tags:
            ps = gmsh.model.addPhysicalGroup(surf_dim, tag)
            gmsh.model.setPhysicalName(surf_dim, ps, phys_name)

        # Embed the surface in the domain
        if embed_in_domain:
            for tag in surf_tag:
                gmsh.model.mesh.embed(surf_dim, [tag], self._dim, self._domain_tag)

        gmsh.model.geo.synchronize()

        # For all surfaces, embed lines in the
        # Do this after all surfaces have been added to get away with a single synchronization
        for tag_ind, pi in enumerate(inds):
            for li in self._data.lines_in_surface[pi]:
                # note the use of indices here, different for lines, polygons and
                # the ordering of surface tags
                gmsh.model.mesh.embed(
                    line_dim, [self._line_tags[li]], surf_dim, surf_tag[tag_ind]
                )

        return surf_tag

    def _add_lines(self, ind: np.ndarray, embed_in_domain: bool) -> list[int]:
        # Helper function to write lines.
        line_tags: list[int] = []

        if ind.size == 0:
            return line_tags

        lines = self._data.lines[:, ind]

        tag = lines[2]

        lines_id = lines[3, :]

        range_id = np.arange(np.amin(lines_id), np.amax(lines_id) + 1)

        line_dim = 1

        has_physical_lines = hasattr(self._data, "physical_lines")

        # Temporary storage of the lines that are to be assigned physical
        # groups
        to_physical_group: list[tuple[int, str, list[int]]] = []

        for i in range_id:
            loc_tags = []
            for mask in np.flatnonzero(lines_id == i):
                p0 = self._point_tags[lines[0, mask]]
                p1 = self._point_tags[lines[1, mask]]
                loc_tags.append(gmsh.model.geo.addLine(p0, p1))
                # Get hold of physical_name, in case we need it
                physical_name = _tag_to_physical_name(tag[mask])

            # Store local tags
            line_tags += loc_tags

            # Add this line to the set of physical groups to be assigned.
            # We do not assign physical groupings inside this for-loop, as this would
            # require multiple costly synchronizations (gmsh style).
            if has_physical_lines and i in self._data.physical_lines:
                to_physical_group.append((i, physical_name, loc_tags))

        # Synchronize model with all new lines
        gmsh.model.geo.synchronize()

        # Assign physical name to the line if specified. This will make gmsh
        # represent the line as a separate physical object in the .msh file
        for (i, physical_name, loc_tags) in to_physical_group:
            phys_group = gmsh.model.addPhysicalGroup(line_dim, loc_tags)
            gmsh.model.setPhysicalName(line_dim, phys_group, f"{physical_name}{i}")

        # Syncronize model, and embed all lines in the domain if specified
        if embed_in_domain:
            gmsh.model.mesh.embed(line_dim, line_tags, self._dim, self._domain_tag)

        return line_tags

    def _add_domain_2d(self) -> int:
        """Write boundary lines, and tie them together to a closed loop. Define domain."""

        # Here we operate on ``FractureNetwork2d.edges``, thus we should use the
        # numerical values of the tag for comparison.
        bound_line_ind = np.argwhere(
            self._data.lines[2] == Tags.DOMAIN_BOUNDARY_LINE.value
        ).ravel()

        bound_line_tags = self._add_lines(bound_line_ind, embed_in_domain=False)

        self._bound_line_tags = bound_line_tags

        loop_tag = gmsh.model.geo.addCurveLoop(bound_line_tags, reorient=True)
        domain_tag = gmsh.model.geo.addPlaneSurface([loop_tag])

        gmsh.model.geo.synchronize()
        phys_group = gmsh.model.addPhysicalGroup(2, [domain_tag])
        gmsh.model.setPhysicalName(2, phys_group, PhysicalNames.DOMAIN.value)
        return domain_tag

    def _add_domain_3d(self) -> int:
        """Write boundary surfaces and domain"""
        if not isinstance(self._data, GmshData3d):
            raise ValueError("Need 3d geometry to specify 3d domain")

        inds = []
        for i, tag in self._data.polygon_tags.items():
            if tag == Tags.DOMAIN_BOUNDARY_SURFACE:
                inds.append(i)

        bound_surf_tags = self._add_polygons_3d(inds, embed_in_domain=False)

        self._bound_surf_tags = bound_surf_tags

        loop_tag = gmsh.model.geo.addSurfaceLoop(bound_surf_tags)
        domain_tag = gmsh.model.geo.addVolume([loop_tag])
        gmsh.model.geo.synchronize()

        phys_group = gmsh.model.addPhysicalGroup(3, [domain_tag])
        gmsh.model.setPhysicalName(3, phys_group, PhysicalNames.DOMAIN.value)
        return domain_tag
