"""
Wing Creator - Blender Add-on
Creates wings from multiple connected airfoil sections.
Compatible with Blender 4.5+
"""

bl_info = {
    "name": "Wing Creator",
    "author": "Wing Creator Contributors",
    "version": (0, 1, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Wing Creator",
    "description": "Create wings from parametric airfoil sections",
    "doc_url": "https://github.com/your-repo/wing-creator",
    "tracker_url": "https://github.com/your-repo/wing-creator/issues",
    "category": "Mesh",
}

import bpy
import bmesh
import math
import re
from bpy.props import (
    StringProperty,
    IntProperty,
    FloatProperty,
    BoolProperty,
    EnumProperty,
    CollectionProperty,
    PointerProperty,
)
from bpy.types import (
    Panel,
    Operator,
    PropertyGroup,
    AddonPreferences,
)


# ---------------------------------------------------------------------------
# NACA Airfoil Generation
# ---------------------------------------------------------------------------

def naca_4digit(code: str, n_points: int = 100) -> list[tuple[float, float]]:
    """
    Generate 2D coordinates for a NACA 4-digit airfoil.
    Returns list of (x, y) tuples forming a closed profile,
    normalized so chord length = 1.0, leading edge at origin,
    trailing edge at (1, 0).
    """
    m = int(code[0]) / 100.0  # max camber
    p = int(code[1]) / 10.0   # location of max camber
    t = int(code[2:4]) / 100.0  # thickness

    xs = [0.5 * (1 - math.cos(math.pi * i / n_points)) for i in range(n_points + 1)]

    def thickness(x):
        return (t / 0.2) * (
            0.2969 * math.sqrt(x)
            - 0.1260 * x
            - 0.3516 * x**2
            + 0.2843 * x**3
            - 0.1015 * x**4
        )

    def camber_and_slope(x):
        if p == 0 or m == 0:
            return 0.0, 0.0
        if x < p:
            yc = (m / p**2) * (2 * p * x - x**2)
            dyc = (2 * m / p**2) * (p - x)
        else:
            yc = (m / (1 - p)**2) * ((1 - 2 * p) + 2 * p * x - x**2)
            dyc = (2 * m / (1 - p)**2) * (p - x)
        return yc, dyc

    upper = []
    lower = []
    for x in xs:
        yt = thickness(x)
        yc, dyc = camber_and_slope(x)
        theta = math.atan(dyc)
        xu = x - yt * math.sin(theta)
        yu = yc + yt * math.cos(theta)
        xl = x + yt * math.sin(theta)
        yl = yc - yt * math.cos(theta)
        upper.append((xu, yu))
        lower.append((xl, yl))

    # Build closed loop: upper surface LE→TE, then lower surface TE→LE
    coords = upper + list(reversed(lower[:-1]))
    return coords


def naca_5digit(code: str, n_points: int = 100) -> list[tuple[float, float]]:
    """
    Generate 2D coordinates for a NACA 5-digit airfoil.
    """
    # Parse 5-digit: e.g. 23012
    cl_design = int(code[0]) * 3 / 20.0
    p = int(code[1]) / 20.0
    # code[2] = 0 (standard) or 1 (reflexed) - we'll handle standard
    t = int(code[3:5]) / 100.0

    # Find m from p using iterative approximation for standard camber
    # Standard 5-digit: uses cubic/quartic camber lines defined by r, k1
    # Simplified: use the numerical coefficients for common cases
    five_digit_params = {
        210: (0.0580, 361.4),
        220: (0.1260, 51.64),
        230: (0.2025, 15.957),
        240: (0.2900, 6.643),
        250: (0.3910, 3.230),
    }
    key = int(code[0:3])
    if key in five_digit_params:
        r, k1 = five_digit_params[key]
    else:
        # fallback to 4-digit style
        return naca_4digit(code[:2] + code[3:5], n_points)

    xs = [0.5 * (1 - math.cos(math.pi * i / n_points)) for i in range(n_points + 1)]

    def thickness(x):
        return (t / 0.2) * (
            0.2969 * math.sqrt(x)
            - 0.1260 * x
            - 0.3516 * x**2
            + 0.2843 * x**3
            - 0.1015 * x**4
        )

    def camber_and_slope(x):
        if x < r:
            yc = (k1 / 6.0) * (x**3 - 3 * r * x**2 + r**2 * (3 - r) * x)
            dyc = (k1 / 6.0) * (3 * x**2 - 6 * r * x + r**2 * (3 - r))
        else:
            yc = (k1 * r**3 / 6.0) * (1 - x)
            dyc = -(k1 * r**3 / 6.0)
        return yc, dyc

    upper = []
    lower = []
    for x in xs:
        yt = thickness(x)
        yc, dyc = camber_and_slope(x)
        theta = math.atan(dyc)
        xu = x - yt * math.sin(theta)
        yu = yc + yt * math.cos(theta)
        xl = x + yt * math.sin(theta)
        yl = yc - yt * math.cos(theta)
        upper.append((xu, yu))
        lower.append((xl, yl))

    coords = upper + list(reversed(lower[:-1]))
    return coords


def generate_airfoil(code: str, chord: float, n_points: int = 60) -> list[tuple[float, float]]:
    """
    Generate airfoil coordinates scaled to the given chord length.
    Orientation: chord runs along +Y, LE at origin, TE at (0, chord).
    """
    code = code.strip().upper()
    digits = re.sub(r'\D', '', code)

    if len(digits) == 4:
        coords = naca_4digit(digits, n_points)
    elif len(digits) == 5:
        coords = naca_5digit(digits, n_points)
    elif len(digits) == 6:
        # Treat 6-series as approximate using last 4 digits for now
        coords = naca_4digit(digits[2:], n_points)
    else:
        # Default to NACA 2412 if invalid
        coords = naca_4digit("2412", n_points)

    # coords are normalized: x in [0,1] = chordwise, y = thickness
    # Remap: Blender Y = chordwise (LE=0 → TE=chord), Blender Z = thickness
    result = []
    for (x, y) in coords:
        by = x * chord          # chordwise along +Y
        bz = y * chord          # thickness along +Z
        result.append((by, bz))
    return result


# ---------------------------------------------------------------------------
# Property Groups
# ---------------------------------------------------------------------------

class WingSection(PropertyGroup):
    """Properties for a single wing section."""
    expanded: BoolProperty(
        name="Expanded",
        default=True,
    )
    airfoil: StringProperty(
        name="Airfoil",
        description="4, 5, or 6-digit NACA designation (e.g. 2412, 23012)",
        default="2412",
        maxlen=8,
    )
    length: FloatProperty(
        name="Section Length",
        description="Span length of this section",
        default=1.0,
        min=0.001,
        soft_max=100.0,
        unit='LENGTH',
    )
    chord: FloatProperty(
        name="Chord",
        description="Chord length for this section",
        default=0.3,
        min=0.001,
        soft_max=100.0,
        unit='LENGTH',
    )


class WingCreatorProperties(PropertyGroup):
    """Root property group for the Wing Creator addon."""

    # ---- UI state ----
    header_expanded: BoolProperty(name="Header Expanded", default=True)
    chord_mode: EnumProperty(
        name="Chord Mode",
        items=[
            ('CONSTANT', "Constant Chord", "Single chord value for entire wing"),
            ('ROOT_TIP', "Root & Tip Chord", "Different chord at root and tip"),
            ('PER_SECTION', "Chord Per Section", "Individual chord per section"),
        ],
        default='CONSTANT',
    )

    # ---- Global chord inputs ----
    chord_constant: FloatProperty(
        name="Chord",
        default=0.3,
        min=0.001,
        soft_max=100.0,
        unit='LENGTH',
    )
    chord_root: FloatProperty(
        name="Root",
        default=0.4,
        min=0.001,
        soft_max=100.0,
        unit='LENGTH',
    )
    chord_tip: FloatProperty(
        name="Tip",
        default=0.2,
        min=0.001,
        soft_max=100.0,
        unit='LENGTH',
    )
    wingspan: FloatProperty(
        name="Wingspan",
        default=2.0,
        min=0.001,
        soft_max=1000.0,
        unit='LENGTH',
    )

    # ---- Sections ----
    num_sections: IntProperty(
        name="Number of Sections",
        default=1,
        min=1,
        max=32,
    )
    sections: CollectionProperty(type=WingSection)

    # ---- Wing state ----
    preview: BoolProperty(
        name="Preview",
        description="Show a live preview of the wing",
        default=False,
    )
    wing_created: BoolProperty(
        name="Wing Created",
        default=False,
    )
    is_editing: BoolProperty(
        name="Is Editing",
        default=False,
    )
    wing_object_name: StringProperty(
        name="Wing Object Name",
        default="",
    )


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class WINGCREATOR_OT_apply_sections(Operator):
    """Apply the number of sections to create/update the section list."""
    bl_idname = "wing_creator.apply_sections"
    bl_label = "Apply Sections"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.wing_creator
        sections = props.sections
        target = props.num_sections

        # Add missing sections
        while len(sections) < target:
            s = sections.add()
            s.airfoil = "2412"
            s.length = 1.0
            s.chord = 0.3
            s.expanded = True

        # Remove extra sections
        while len(sections) > target:
            sections.remove(len(sections) - 1)

        return {'FINISHED'}


class WINGCREATOR_OT_create(Operator):
    """Create the wing geometry."""
    bl_idname = "wing_creator.create"
    bl_label = "Create Wing"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.wing_creator
        self._build_wing(context, props)
        props.wing_created = True
        props.is_editing = False
        return {'FINISHED'}

    def _build_wing(self, context, props):
        # Remove old wing if updating
        if props.wing_object_name and props.wing_object_name in bpy.data.objects:
            old_obj = bpy.data.objects[props.wing_object_name]
            bpy.data.meshes.remove(old_obj.data, do_unlink=True)

        me = bpy.data.meshes.new("WingMesh")
        obj = bpy.data.objects.new("Wing", me)
        context.collection.objects.link(obj)

        bm = bmesh.new()

        sections = props.sections
        chord_mode = props.chord_mode
        num_sections = len(sections)

        x_offset = 0.0  # current span position
        prev_verts = None

        for i, section in enumerate(sections):
            # Determine chord for this section
            if chord_mode == 'CONSTANT':
                chord_root_val = props.chord_constant
                chord_tip_val = props.chord_constant
            elif chord_mode == 'ROOT_TIP':
                t_start = i / num_sections
                t_end = (i + 1) / num_sections
                chord_root_val = props.chord_root + t_start * (props.chord_tip - props.chord_root)
                chord_tip_val = props.chord_root + t_end * (props.chord_tip - props.chord_root)
            else:  # PER_SECTION
                chord_root_val = section.chord
                chord_tip_val = section.chord

            span = section.length

            # Generate airfoil profiles at root and tip of this section
            profile_root = generate_airfoil(section.airfoil, chord_root_val)
            profile_tip = generate_airfoil(section.airfoil, chord_tip_val)

            n_pts = len(profile_root)

            # Create root ring vertices at x_offset
            root_verts = []
            for (by, bz) in profile_root:
                v = bm.verts.new((x_offset, by, bz))
                root_verts.append(v)

            # Create tip ring vertices at x_offset + span
            tip_verts = []
            for (by, bz) in profile_tip:
                v = bm.verts.new((x_offset + span, by, bz))
                tip_verts.append(v)

            bm.verts.ensure_lookup_table()

            # Connect root and tip with quad faces
            for j in range(n_pts):
                j_next = (j + 1) % n_pts
                try:
                    bm.faces.new([
                        root_verts[j],
                        root_verts[j_next],
                        tip_verts[j_next],
                        tip_verts[j],
                    ])
                except Exception:
                    pass

            # If we have a previous section, stitch the shared edge (they share the same X plane)
            # Since we create separate root verts per section (they overlap), merge by distance later
            prev_verts = tip_verts
            x_offset += span

        # Cap the leading and trailing edges (fill ends)
        # Use bmesh.ops.recalc_face_normals for clean normals
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

        # Remove duplicate verts at section joints
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)

        bm.to_mesh(me)
        bm.free()
        me.update()

        # Set as active
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj

        props.wing_object_name = obj.name
        self.report({'INFO'}, f"Wing created: {obj.name}")


class WINGCREATOR_OT_edit(Operator):
    """Switch wing to edit mode, re-enabling inputs."""
    bl_idname = "wing_creator.edit"
    bl_label = "Edit Wing"

    def execute(self, context):
        props = context.scene.wing_creator
        props.is_editing = True
        return {'FINISHED'}


class WINGCREATOR_OT_update(Operator):
    """Update the wing geometry after editing."""
    bl_idname = "wing_creator.update"
    bl_label = "Update Wing"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.wing_creator
        creator = WINGCREATOR_OT_create(bl_idname="wing_creator.create", bl_label="")
        creator._build_wing(context, props)
        props.wing_created = True
        props.is_editing = False
        return {'FINISHED'}


class WINGCREATOR_OT_check_updates(Operator):
    """Open the GitHub releases page to check for updates."""
    bl_idname = "wing_creator.check_updates"
    bl_label = "Check for Updates"

    def execute(self, context):
        import webbrowser
        webbrowser.open("https://github.com/your-repo/wing-creator/releases")
        self.report({'INFO'}, "Opened GitHub releases page in browser.")
        return {'FINISHED'}


class WINGCREATOR_OT_open_docs(Operator):
    """Open the GitHub documentation page."""
    bl_idname = "wing_creator.open_docs"
    bl_label = "Documentation"

    def execute(self, context):
        import webbrowser
        webbrowser.open("https://github.com/your-repo/wing-creator")
        self.report({'INFO'}, "Opened documentation in browser.")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# N-Panel UI
# ---------------------------------------------------------------------------

class WINGCREATOR_PT_main(Panel):
    bl_label = "Wing Creator"
    bl_idname = "WINGCREATOR_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Wing Creator"

    def draw(self, context):
        layout = self.layout
        props = context.scene.wing_creator
        locked = props.wing_created and not props.is_editing

        # ---- HEADER -------------------------------------------------------
        box = layout.box()
        row = box.row()
        row.prop(
            props, "header_expanded",
            icon='TRIA_DOWN' if props.header_expanded else 'TRIA_RIGHT',
            icon_only=True, emboss=False,
        )
        row.label(text="Wing Creator", icon='MATFLUID')

        if props.header_expanded:
            row = box.row(align=True)
            row.operator("wing_creator.open_docs", icon='URL', text="Documentation")
            row.operator("wing_creator.check_updates", icon='FILE_REFRESH', text="Check Updates")

        layout.separator()

        # ---- CHORD MODE ---------------------------------------------------
        col = layout.column()
        col.enabled = not locked
        col.label(text="Chord Type:")
        col.prop(props, "chord_mode", expand=True)
        col.separator()

        if props.chord_mode == 'CONSTANT':
            row = col.row(align=True)
            row.prop(props, "chord_constant")
            col.prop(props, "wingspan")

        elif props.chord_mode == 'ROOT_TIP':
            row = col.row(align=True)
            row.prop(props, "chord_root")
            row.prop(props, "chord_tip")
            col.prop(props, "wingspan")

        # PER_SECTION: chord shown in each section panel below
        elif props.chord_mode == 'PER_SECTION':
            col.label(text="(Chord defined per section below)", icon='INFO')

        layout.separator()

        # ---- SECTION COUNT ------------------------------------------------
        row = layout.row(align=True)
        row.enabled = not locked
        row.prop(props, "num_sections")
        row.operator("wing_creator.apply_sections", icon='CHECKMARK', text="")

        layout.separator()

        # ---- SECTION PANELS -----------------------------------------------
        for i, section in enumerate(props.sections):
            box = layout.box()

            # Section header row
            row = box.row()
            row.prop(
                section, "expanded",
                icon='TRIA_DOWN' if section.expanded else 'TRIA_RIGHT',
                icon_only=True, emboss=False,
            )
            row.label(text=f"Section {i + 1}", icon='MOD_ARRAY')

            if section.expanded:
                col = box.column()
                col.enabled = not locked

                col.prop(section, "airfoil", text="Airfoil (NACA)")

                if props.chord_mode == 'PER_SECTION':
                    col.prop(section, "chord", text="Chord")

                col.prop(section, "length", text="Section Length")

        layout.separator()

        # ---- PREVIEW ------------------------------------------------------
        row = layout.row()
        row.prop(props, "preview", text="Preview (coming soon)", icon='HIDE_OFF')

        layout.separator()

        # ---- CREATE / EDIT / UPDATE ---------------------------------------
        if not props.wing_created:
            layout.operator("wing_creator.create", icon='MESH_DATA', text="Create")
        elif props.is_editing:
            layout.operator("wing_creator.update", icon='FILE_REFRESH', text="Update")
        else:
            layout.operator("wing_creator.edit", icon='GREASEPENCIL', text="Edit")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    WingSection,
    WingCreatorProperties,
    WINGCREATOR_OT_apply_sections,
    WINGCREATOR_OT_create,
    WINGCREATOR_OT_edit,
    WINGCREATOR_OT_update,
    WINGCREATOR_OT_check_updates,
    WINGCREATOR_OT_open_docs,
    WINGCREATOR_PT_main,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.wing_creator = PointerProperty(type=WingCreatorProperties)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.wing_creator


if __name__ == "__main__":
    register()
