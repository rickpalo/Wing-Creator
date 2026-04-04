"""
Wing Creator — Blender Add-on  v0.5.0
Creates wings from parametric airfoil sections.
Blender 4.5+  |  github.com/rickpalo/Wing-Creator/
"""

bl_info = {
    "name": "Wing Creator",
    "author": "Wing Creator Contributors",
    "version": (0, 5, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Wing Creator",
    "description": "Create wings from parametric airfoil sections",
    "doc_url": "https://github.com/rickpalo/Wing-Creator/",
    "tracker_url": "https://github.com/rickpalo/Wing-Creator/issues",
    "category": "Mesh",
}

import bpy
import bmesh
import json
import math
import os
import re
import shutil
import tempfile
import urllib.request
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty,
    FloatProperty, IntProperty, PointerProperty, StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADDON_VERSION   = (0, 5, 0)
GITHUB_API_URL  = "https://api.github.com/repos/rickpalo/Wing-Creator/releases/latest"
GITHUB_REPO_URL = "https://github.com/rickpalo/Wing-Creator/"
WING_TAG        = "wing_creator_data"

# ---------------------------------------------------------------------------
# .dat file cache
# ---------------------------------------------------------------------------

_DAT_CACHE: dict = {}


def load_dat_file(path: str):
    if not path:
        return None
    try:
        abs_path = os.path.abspath(bpy.path.abspath(path))
    except Exception:
        return None
    if abs_path in _DAT_CACHE:
        return _DAT_CACHE[abs_path]
    try:
        with open(abs_path, 'r') as fh:
            raw = fh.readlines()
    except OSError:
        return None

    rows = []
    for line in raw:
        line = line.strip()
        if not line or line.startswith(('#', '!', '%')):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue

    if len(rows) < 4:
        return None

    if rows[0][0] > 1.5:          # Lednicer format
        nu = int(round(rows[0][0]))
        nl = int(round(rows[0][1]))
        upper = rows[1: 1 + nu]
        lower = rows[1 + nu: 1 + nu + nl]
        coords = upper + list(reversed(lower))
    else:
        coords = rows

    xs = [p[0] for p in coords]
    xmin, xmax = min(xs), max(xs)
    span = (xmax - xmin) or 1.0
    coords = [((x - xmin) / span, y / span) for x, y in coords]
    _DAT_CACHE[abs_path] = coords
    return coords


# ---------------------------------------------------------------------------
# NACA generators
# ---------------------------------------------------------------------------

def _cosine_xs(n: int):
    return [0.5 * (1.0 - math.cos(math.pi * i / n)) for i in range(n + 1)]


def naca_4digit(code: str, n: int = 80):
    m = int(code[0]) / 100.0
    p = int(code[1]) / 10.0
    t = int(code[2:4]) / 100.0
    xs = _cosine_xs(n)

    def thick(x):
        return (t / 0.2) * (0.2969 * math.sqrt(max(x, 0))
                             - 0.1260 * x - 0.3516 * x**2
                             + 0.2843 * x**3 - 0.1015 * x**4)

    def camber(x):
        if not (p and m):
            return 0.0, 0.0
        if x < p:
            return (m / p**2) * (2*p*x - x**2), (2*m / p**2) * (p - x)
        return ((m / (1-p)**2) * ((1-2*p) + 2*p*x - x**2),
                (2*m / (1-p)**2) * (p - x))

    up, lo = [], []
    for x in xs:
        yt = thick(x)
        yc, dyc = camber(x)
        th = math.atan(dyc)
        up.append((x - yt * math.sin(th), yc + yt * math.cos(th)))
        lo.append((x + yt * math.sin(th), yc - yt * math.cos(th)))
    return up + list(reversed(lo[:-1]))


def naca_5digit(code: str, n: int = 80):
    t = int(code[3:5]) / 100.0
    lut = {210: (0.0580, 361.4), 220: (0.1260, 51.64),
           230: (0.2025, 15.957), 240: (0.2900, 6.643),
           250: (0.3910, 3.230)}
    key = int(code[:3])
    if key not in lut:
        return naca_4digit(code[:2] + code[3:5], n)
    r, k1 = lut[key]
    xs = _cosine_xs(n)

    def thick(x):
        return (t / 0.2) * (0.2969 * math.sqrt(max(x, 0))
                             - 0.1260 * x - 0.3516 * x**2
                             + 0.2843 * x**3 - 0.1015 * x**4)

    def camber(x):
        if x < r:
            return ((k1/6) * (x**3 - 3*r*x**2 + r**2*(3-r)*x),
                    (k1/6) * (3*x**2 - 6*r*x + r**2*(3-r)))
        return (k1*r**3/6) * (1-x), -(k1*r**3/6)

    up, lo = [], []
    for x in xs:
        yt = thick(x)
        yc, dyc = camber(x)
        th = math.atan(dyc)
        up.append((x - yt * math.sin(th), yc + yt * math.cos(th)))
        lo.append((x + yt * math.sin(th), yc - yt * math.cos(th)))
    return up + list(reversed(lo[:-1]))


def get_airfoil_coords(code: str, dat_path: str, n: int = 80):
    if dat_path:
        c = load_dat_file(dat_path)
        if c:
            return c
    digits = re.sub(r'\D', '', code.strip().upper())
    if len(digits) == 4:
        return naca_4digit(digits, n)
    if len(digits) == 5:
        return naca_5digit(digits, n)
    if len(digits) == 6:
        return naca_4digit(digits[2:], n)
    return naca_4digit("2412", n)


def interp_profiles(prof_a, prof_b, t: float):
    """
    Linear interpolation between two normalized airfoil profiles.
    Both must have the same number of points (resampled to min length if not).
    t=0 → prof_a, t=1 → prof_b.
    """
    n = min(len(prof_a), len(prof_b))
    # Resample to n points if lengths differ
    def resample(prof, n):
        if len(prof) == n:
            return prof
        out = []
        for i in range(n):
            f = i / (n - 1) * (len(prof) - 1)
            lo = int(f)
            hi = min(lo + 1, len(prof) - 1)
            frac = f - lo
            ox = prof[lo][0] + frac * (prof[hi][0] - prof[lo][0])
            oy = prof[lo][1] + frac * (prof[hi][1] - prof[lo][1])
            out.append((ox, oy))
        return out
    a = resample(prof_a, n)
    b = resample(prof_b, n)
    return [(a[i][0] * (1-t) + b[i][0] * t,
             a[i][1] * (1-t) + b[i][1] * t) for i in range(n)]


# ---------------------------------------------------------------------------
# Section data extraction
# ---------------------------------------------------------------------------

def _section_dicts(props):
    """
    Return a list of dicts, one per geometric section, fully resolved.
    Each dict has:
      span, c_root, c_tip,
      prof_root [(nx,ny)...], prof_tip [(nx,ny)...],
      sweep_deg, dihedral_deg
    """
    mode = props.chord_mode
    res  = props.resolution

    if mode == 'CONSTANT':
        raw = get_airfoil_coords(props.airfoil_constant,
                                 props.dat_path_constant, res)
        return [{'span':        props.wingspan,
                 'c_root':      props.chord_constant,
                 'c_tip':       props.chord_constant,
                 'prof_root':   raw,
                 'prof_tip':    raw,
                 'sweep_deg':   props.sweep_constant,
                 'dihedral_deg':props.dihedral_constant}]

    if mode == 'ROOT_TIP':
        raw = get_airfoil_coords(props.airfoil_root_tip,
                                 props.dat_path_root_tip, res)
        return [{'span':        props.wingspan,
                 'c_root':      props.chord_root,
                 'c_tip':       props.chord_tip,
                 'prof_root':   raw,
                 'prof_tip':    raw,
                 'sweep_deg':   props.sweep_root_tip,
                 'dihedral_deg':props.dihedral_root_tip}]

    # PER_SECTION — resolve chord/airfoil continuity between sections
    out = []
    prev_c_tip      = None
    prev_af_code    = None
    prev_dat_path   = None

    for s in props.sections:
        s_mode = s.section_chord_mode   # 'CONSTANT' or 'ROOT_TIP'

        # ---- chord root (propagated from previous section tip) ----
        if prev_c_tip is not None:
            c_root = prev_c_tip          # locked — from prior section
        else:
            c_root = s.chord_root        # first section — user editable

        # ---- chord tip ----
        if s_mode == 'CONSTANT':
            c_tip = s.chord_root         # same as root
        else:
            c_tip = s.chord_tip

        # ---- airfoil root ----
        if not s.constant_airfoil:
            if prev_af_code is not None:
                # locked — propagated
                af_root_code = prev_af_code
                af_root_dat  = prev_dat_path
            else:
                af_root_code = s.airfoil_root
                af_root_dat  = s.dat_path_root
            # tip
            af_tip_code = s.airfoil_tip
            af_tip_dat  = s.dat_path_tip
        else:
            # constant airfoil: single code for both ends
            if prev_af_code is not None:
                af_root_code = prev_af_code
                af_root_dat  = prev_dat_path
            else:
                af_root_code = s.airfoil_root
                af_root_dat  = s.dat_path_root
            af_tip_code = af_root_code
            af_tip_dat  = af_root_dat

        prof_root = get_airfoil_coords(af_root_code, af_root_dat, res)
        prof_tip  = get_airfoil_coords(af_tip_code,  af_tip_dat,  res)

        out.append({'span':        s.length,
                    'c_root':      c_root,
                    'c_tip':       c_tip,
                    'prof_root':   prof_root,
                    'prof_tip':    prof_tip,
                    'sweep_deg':   s.sweep,
                    'dihedral_deg':s.dihedral})

        # propagate to next section
        prev_c_tip    = c_tip
        prev_af_code  = af_tip_code
        prev_dat_path = af_tip_dat

    return out


# ---------------------------------------------------------------------------
# Wing mesh builder
# ---------------------------------------------------------------------------

def build_wing_mesh(props, mesh) -> None:
    """
    Build wing geometry into `mesh`.

    +X  spanwise (root → tip)
    +Y  chordwise (LE → TE)  — quarter-chord kept at section's local Y=0
    +Z  thickness / up

    Sweep and dihedral stored as plain degrees (no subtype='ANGLE' double-
    conversion).  math.radians() is called exactly once here.
    """
    bm  = bmesh.new()
    sds = _section_dicts(props)

    if not sds:
        bm.to_mesh(mesh); bm.free(); return

    ox = props.centerline_offset if props.use_mirror else 0.0
    oy = 0.0
    oz = 0.0

    for sd in sds:
        sw  = math.radians(sd['sweep_deg'])
        dh  = math.radians(sd['dihedral_deg'])
        span   = sd['span']
        c_root = sd['c_root']
        c_tip  = sd['c_tip']

        # Tip position relative to root — degrees → radians done above
        dx = span * math.cos(dh) * math.cos(sw)
        dy = span * math.cos(dh) * math.sin(sw)
        dz = span * math.sin(dh)
        tx, ty, tz = ox + dx, oy + dy, oz + dz

        def ring(origin_x, origin_y, origin_z, chord, prof):
            qc = chord * 0.25
            return [bm.verts.new((origin_x,
                                  origin_y + nx * chord - qc,
                                  origin_z + ny * chord))
                    for nx, ny in prof]

        rv = ring(ox, oy, oz, c_root, sd['prof_root'])
        tv = ring(tx, ty, tz, c_tip,  sd['prof_tip'])
        bm.verts.ensure_lookup_table()

        n = len(rv)
        for j in range(n):
            j1 = (j + 1) % n
            try:
                bm.faces.new([rv[j], rv[j1], tv[j1], tv[j]])
            except Exception:
                pass

        ox, oy, oz = tx, ty, tz

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bm.to_mesh(mesh); bm.free(); mesh.update()


# ---------------------------------------------------------------------------
# Empty / hierarchy helpers
# ---------------------------------------------------------------------------

def _root_chord(props) -> float:
    mode = props.chord_mode
    if mode == 'CONSTANT':  return props.chord_constant
    if mode == 'ROOT_TIP':  return props.chord_root
    if props.sections:      return props.sections[0].chord_root
    return 0.3


def create_or_update_empty(context, props, wing_obj):
    chord = _root_chord(props)
    qc    = chord * 0.25
    base  = props.wing_name.strip() or "Wing"
    ename = base + "_Root"

    empty_name = props.empty_object_name
    if empty_name and empty_name in bpy.data.objects:
        empty = bpy.data.objects[empty_name]
    else:
        empty = bpy.data.objects.new(ename, None)
        context.collection.objects.link(empty)
        props.empty_object_name = empty.name

    empty.name               = ename
    empty.empty_display_type = 'CUBE'
    empty.empty_display_size = qc
    empty.location           = (0.0, qc, 0.0)

    # Parent wing to empty
    if wing_obj.parent != empty:
        mat = wing_obj.matrix_world.copy()
        wing_obj.parent = empty
        wing_obj.matrix_parent_inverse = empty.matrix_world.inverted()
        wing_obj.matrix_world = mat

    wing_obj.name = base

    # Wing mesh is non-selectable
    wing_obj.hide_select = True

    # Mirror modifier
    MOD = "WingCreator_Mirror"
    if not props.use_mirror:
        mod = wing_obj.modifiers.get(MOD)
        if mod:
            wing_obj.modifiers.remove(mod)
    else:
        mod = wing_obj.modifiers.get(MOD)
        if mod is None:
            mod = wing_obj.modifiers.new(name=MOD, type='MIRROR')
        mod.use_axis[0]      = True
        mod.mirror_object    = empty
        mod.use_mirror_merge = True
        mod.merge_threshold  = 0.001

    return empty


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _section_to_dict(s) -> dict:
    return {
        'section_chord_mode':  s.section_chord_mode,
        'chord_root':          s.chord_root,
        'chord_tip':           s.chord_tip,
        'constant_airfoil':    s.constant_airfoil,
        'airfoil_root':        s.airfoil_root,
        'dat_path_root':       s.dat_path_root,
        'airfoil_tip':         s.airfoil_tip,
        'dat_path_tip':        s.dat_path_tip,
        'length':              s.length,
        'sweep':               s.sweep,
        'dihedral':            s.dihedral,
        'expanded':            s.expanded,
    }


def props_to_dict(props) -> dict:
    return {
        'wing_name':          props.wing_name,
        'resolution':         props.resolution,
        'use_mirror':         props.use_mirror,
        'centerline_offset':  props.centerline_offset,
        'chord_mode':         props.chord_mode,
        'chord_constant':     props.chord_constant,
        'airfoil_constant':   props.airfoil_constant,
        'dat_path_constant':  props.dat_path_constant,
        'wingspan':           props.wingspan,
        'sweep_constant':     props.sweep_constant,
        'dihedral_constant':  props.dihedral_constant,
        'chord_root':         props.chord_root,
        'chord_tip':          props.chord_tip,
        'airfoil_root_tip':   props.airfoil_root_tip,
        'dat_path_root_tip':  props.dat_path_root_tip,
        'sweep_root_tip':     props.sweep_root_tip,
        'dihedral_root_tip':  props.dihedral_root_tip,
        'num_sections':       props.num_sections,
        'sections':           [_section_to_dict(s) for s in props.sections],
        'wing_created':       props.wing_created,
        'empty_object_name':  props.empty_object_name,
    }


def _load_section(s, sd: dict):
    s.section_chord_mode = sd.get('section_chord_mode', 'CONSTANT')
    s.chord_root         = sd.get('chord_root',  0.3)
    s.chord_tip          = sd.get('chord_tip',   0.2)
    s.constant_airfoil   = sd.get('constant_airfoil', True)
    s.airfoil_root       = sd.get('airfoil_root', '2412')
    s.dat_path_root      = sd.get('dat_path_root', '')
    s.airfoil_tip        = sd.get('airfoil_tip',  '2412')
    s.dat_path_tip       = sd.get('dat_path_tip',  '')
    s.length             = sd.get('length',   1.0)
    s.sweep              = sd.get('sweep',    0.0)
    s.dihedral           = sd.get('dihedral', 0.0)
    s.expanded           = sd.get('expanded', True)


def dict_to_props(data: dict, props) -> None:
    _RESTORING[0] = True
    try:
        props.wing_name          = data.get('wing_name', 'Wing')
        props.resolution         = data.get('resolution', 80)
        props.use_mirror         = data.get('use_mirror', False)
        props.centerline_offset  = data.get('centerline_offset', 0.0)
        props.chord_mode         = data.get('chord_mode', 'CONSTANT')
        props.chord_constant     = data.get('chord_constant', 0.3)
        props.airfoil_constant   = data.get('airfoil_constant', '2412')
        props.dat_path_constant  = data.get('dat_path_constant', '')
        props.wingspan           = data.get('wingspan', 2.0)
        props.sweep_constant     = data.get('sweep_constant', 0.0)
        props.dihedral_constant  = data.get('dihedral_constant', 0.0)
        props.chord_root         = data.get('chord_root', 0.4)
        props.chord_tip          = data.get('chord_tip', 0.2)
        props.airfoil_root_tip   = data.get('airfoil_root_tip', '2412')
        props.dat_path_root_tip  = data.get('dat_path_root_tip', '')
        props.sweep_root_tip     = data.get('sweep_root_tip', 0.0)
        props.dihedral_root_tip  = data.get('dihedral_root_tip', 0.0)
        props.num_sections       = data.get('num_sections', 1)
        props.wing_created       = data.get('wing_created', True)
        props.is_editing         = False
        props.empty_object_name  = data.get('empty_object_name', '')
        props.sections.clear()
        for sd in data.get('sections', []):
            _load_section(props.sections.add(), sd)
    finally:
        _RESTORING[0] = False


def reset_props(props) -> None:
    _RESTORING[0] = True
    try:
        props.wing_name = 'Wing';  props.resolution = 80
        props.use_mirror = False;  props.centerline_offset = 0.0
        props.chord_mode = 'CONSTANT'
        props.chord_constant = 0.3;  props.airfoil_constant = '2412'
        props.dat_path_constant = '';  props.wingspan = 2.0
        props.sweep_constant = 0.0;  props.dihedral_constant = 0.0
        props.chord_root = 0.4;  props.chord_tip = 0.2
        props.airfoil_root_tip = '2412';  props.dat_path_root_tip = ''
        props.sweep_root_tip = 0.0;  props.dihedral_root_tip = 0.0
        props.num_sections = 1;  props.sections.clear()
        props.wing_created = False;  props.is_editing = False
        props.wing_object_name = '';  props.empty_object_name = ''
        props.preview = False
        props.update_available = False
        props.latest_version = '';  props.latest_zip_url = ''
    finally:
        _RESTORING[0] = False


def save_to_obj(props, obj):
    obj[WING_TAG] = json.dumps(props_to_dict(props))


def load_from_obj(obj, props) -> bool:
    raw = obj.get(WING_TAG)
    if raw is None:
        return False
    try:
        data = json.loads(raw)
    except Exception:
        return False
    dict_to_props(data, props)
    # Find the wing child
    for child in obj.children:
        if child.get(WING_TAG):
            props.wing_object_name = child.name
            return True
    # Fallback: obj itself might be the wing (legacy)
    props.wing_object_name = obj.name
    return True


# ---------------------------------------------------------------------------
# Preview / selection guard
# ---------------------------------------------------------------------------

_RESTORING        = [False]
_LAST_ACTIVE_NAME = [None]


def _trigger_preview(self, context):
    if _RESTORING[0]:
        return
    props = context.scene.wing_creator

    # Sync sections to 1 when not in PER_SECTION mode
    if props.chord_mode != 'PER_SECTION':
        _RESTORING[0] = True
        try:
            props.num_sections = 1
            while len(props.sections) > 1:
                props.sections.remove(len(props.sections) - 1)
            if not props.sections:
                _add_default_section(props.sections)
        finally:
            _RESTORING[0] = False

    if not props.preview:
        return
    name = props.wing_object_name
    if name and name in bpy.data.objects:
        obj = bpy.data.objects[name]
        build_wing_mesh(props, obj.data)
        save_to_obj(props, obj)


def _pu(self, context):
    _trigger_preview(self, context)


def _add_default_section(sections):
    s = sections.add()
    s.section_chord_mode = 'CONSTANT'
    s.chord_root  = 0.3;  s.chord_tip  = 0.2
    s.constant_airfoil = True
    s.airfoil_root = '2412';  s.dat_path_root = ''
    s.airfoil_tip  = '2412';  s.dat_path_tip  = ''
    s.length = 1.0;  s.sweep = 0.0;  s.dihedral = 0.0;  s.expanded = True


# ---------------------------------------------------------------------------
# Property groups
# ---------------------------------------------------------------------------

class WingSection(PropertyGroup):
    expanded: BoolProperty(name="Expanded", default=True)

    # Per-section chord sub-mode
    section_chord_mode: EnumProperty(
        name="Section Chord",
        items=[('CONSTANT', "Constant Chord", "Same chord root to tip"),
               ('ROOT_TIP', "Root & Tip",     "Taper within this section")],
        default='CONSTANT', update=_pu)

    # Chord values — chord_root may be locked (propagated from prev section)
    chord_root: FloatProperty(name="Root Chord", default=0.3,
                              min=0.001, soft_max=100.0, unit='LENGTH', update=_pu)
    chord_tip:  FloatProperty(name="Tip Chord",  default=0.2,
                              min=0.001, soft_max=100.0, unit='LENGTH', update=_pu)

    # Airfoil: single or per-end
    constant_airfoil: BoolProperty(
        name="Constant Airfoil",
        description="Use the same airfoil shape from root to tip of this section",
        default=True, update=_pu)

    # Root airfoil (may be locked when propagated)
    airfoil_root:   StringProperty(name="Root Airfoil", default="2412",
                                   maxlen=8, update=_pu)
    dat_path_root:  StringProperty(name="Root .dat",    default="",
                                   subtype='FILE_PATH', update=_pu)
    # Tip airfoil (only active when constant_airfoil is False)
    airfoil_tip:    StringProperty(name="Tip Airfoil",  default="2412",
                                   maxlen=8, update=_pu)
    dat_path_tip:   StringProperty(name="Tip .dat",     default="",
                                   subtype='FILE_PATH', update=_pu)

    length:   FloatProperty(name="Section Length", default=1.0,
                            min=0.001, soft_max=100.0, unit='LENGTH', update=_pu)
    # Degrees stored as plain floats — math.radians() called once in builder
    sweep:    FloatProperty(name="Sweep °",    default=0.0,
                            min=-89.0, max=89.0, update=_pu)
    dihedral: FloatProperty(name="Dihedral °", default=0.0,
                            min=-89.0, max=89.0, update=_pu)


class WingCreatorProperties(PropertyGroup):
    header_expanded: BoolProperty(name="Header Expanded", default=True)

    wing_name:  StringProperty(name="Wing Name",  default="Wing", update=_pu)
    resolution: IntProperty(name="Resolution",    default=80, min=8, max=512, update=_pu)
    use_mirror: BoolProperty(name="Mirror Across X", default=False, update=_pu)
    centerline_offset: FloatProperty(
        name="Offset from Centerline", default=0.0,
        min=0.0, soft_max=10.0, unit='LENGTH', update=_pu)

    chord_mode: EnumProperty(
        name="Chord Type",
        items=[('CONSTANT',    "Constant Chord",   "Single chord for whole wing"),
               ('ROOT_TIP',    "Root & Tip Chord",  "Chord tapers root→tip"),
               ('PER_SECTION', "Chord Per Section", "Individual chord per section")],
        default='CONSTANT', update=_pu)

    # Constant chord
    chord_constant:    FloatProperty(name="Chord",   default=0.3, min=0.001,
                                     soft_max=100.0, unit='LENGTH', update=_pu)
    airfoil_constant:  StringProperty(name="Airfoil", default="2412", maxlen=8, update=_pu)
    dat_path_constant: StringProperty(name=".dat",    default="", subtype='FILE_PATH', update=_pu)
    wingspan:          FloatProperty(name="Wingspan", default=2.0, min=0.001,
                                     soft_max=1000.0, unit='LENGTH', update=_pu)
    # Degrees — plain float, no subtype='ANGLE'
    sweep_constant:    FloatProperty(name="Sweep °",    default=0.0, min=-89.0, max=89.0, update=_pu)
    dihedral_constant: FloatProperty(name="Dihedral °", default=0.0, min=-89.0, max=89.0, update=_pu)

    # Root & Tip chord
    chord_root:        FloatProperty(name="Root",    default=0.4, min=0.001,
                                     soft_max=100.0, unit='LENGTH', update=_pu)
    chord_tip:         FloatProperty(name="Tip",     default=0.2, min=0.001,
                                     soft_max=100.0, unit='LENGTH', update=_pu)
    airfoil_root_tip:  StringProperty(name="Airfoil", default="2412", maxlen=8, update=_pu)
    dat_path_root_tip: StringProperty(name=".dat",    default="", subtype='FILE_PATH', update=_pu)
    sweep_root_tip:    FloatProperty(name="Sweep °",    default=0.0, min=-89.0, max=89.0, update=_pu)
    dihedral_root_tip: FloatProperty(name="Dihedral °", default=0.0, min=-89.0, max=89.0, update=_pu)

    num_sections: IntProperty(name="Number of Sections", default=1, min=1, max=32)
    sections:     CollectionProperty(type=WingSection)

    preview:           BoolProperty(name="Live Preview", default=False, update=_pu)
    wing_created:      BoolProperty(name="Wing Created",  default=False)
    is_editing:        BoolProperty(name="Is Editing",    default=False)
    wing_object_name:  StringProperty(name="Wing Object", default="")
    empty_object_name: StringProperty(name="Empty Object", default="")

    update_available:  BoolProperty(name="Update Available", default=False)
    latest_version:    StringProperty(name="Latest Version",  default="")
    latest_zip_url:    StringProperty(name="Latest Zip URL",  default="")


# ---------------------------------------------------------------------------
# Shared UI helpers
# ---------------------------------------------------------------------------

def _ensure_wing_object(context, props):
    name = props.wing_object_name
    if name and name in bpy.data.objects:
        return bpy.data.objects[name]
    me  = bpy.data.meshes.new("WingMesh")
    obj = bpy.data.objects.new("Wing", me)
    context.collection.objects.link(obj)
    props.wing_object_name = obj.name
    return obj


def draw_airfoil_block(layout, owner, af_attr, dat_attr, target_str, locked,
                       label="Airfoil (NACA)"):
    col = layout.column(align=True)
    col.enabled = not locked
    dat_val = getattr(owner, dat_attr)
    if dat_val:
        row = col.row(align=True)
        row.label(text=os.path.basename(dat_val), icon='FILE')
        op = row.operator("wing_creator.clear_dat", icon='X', text="")
        op.target = target_str
    else:
        col.prop(owner, af_attr, text=label)
    op = col.operator("wing_creator.import_dat", icon='IMPORT', text="Import .dat File")
    op.target = target_str


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class WINGCREATOR_OT_apply_sections(Operator):
    bl_idname = "wing_creator.apply_sections"
    bl_label  = "Apply Sections"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.wing_creator
        secs  = props.sections
        while len(secs) < props.num_sections:
            _add_default_section(secs)
        while len(secs) > props.num_sections:
            secs.remove(len(secs) - 1)
        # Propagate chord from previous tip
        _propagate_section_chords(props)
        _trigger_preview(self, context)
        return {'FINISHED'}


def _propagate_section_chords(props):
    """
    For PER_SECTION mode, sync chord_root of each section (beyond the first)
    to the chord_tip (or chord_root for CONSTANT) of the preceding section.
    Also sync airfoil_root when constant_airfoil is False.
    This is called after any section change.
    """
    if props.chord_mode != 'PER_SECTION':
        return
    _RESTORING[0] = True
    try:
        prev_tip_chord = None
        prev_af_code   = None
        prev_dat       = None
        for i, s in enumerate(props.sections):
            if i > 0 and prev_tip_chord is not None:
                s.chord_root = prev_tip_chord
                if not s.constant_airfoil and prev_af_code is not None:
                    s.airfoil_root  = prev_af_code
                    s.dat_path_root = prev_dat
            # Determine this section's tip values for the next iteration
            if s.section_chord_mode == 'CONSTANT':
                prev_tip_chord = s.chord_root
            else:
                prev_tip_chord = s.chord_tip
            if not s.constant_airfoil:
                prev_af_code = s.airfoil_tip
                prev_dat     = s.dat_path_tip
            else:
                prev_af_code = s.airfoil_root
                prev_dat     = s.dat_path_root
    finally:
        _RESTORING[0] = False


class WINGCREATOR_OT_import_dat(Operator):
    bl_idname  = "wing_creator.import_dat"
    bl_label   = "Import .dat Airfoil"
    bl_options = {'REGISTER', 'UNDO'}

    target:      StringProperty(name="Target", default="CONSTANT")
    filepath:    StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default="*.dat;*.txt", options={'HIDDEN'})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        props  = context.scene.wing_creator
        coords = load_dat_file(self.filepath)
        if coords is None:
            self.report({'ERROR'}, f"Could not parse: {self.filepath}")
            return {'CANCELLED'}

        if self.target == 'CONSTANT':
            props.dat_path_constant = self.filepath
        elif self.target == 'ROOT_TIP':
            props.dat_path_root_tip = self.filepath
        else:
            # Format: "N:root" or "N:tip" or "N" (legacy constant)
            parts = self.target.split(':')
            idx = int(parts[0])
            end = parts[1] if len(parts) > 1 else 'root'
            try:
                s = props.sections[idx]
                if end == 'tip':
                    s.dat_path_tip  = self.filepath
                else:
                    s.dat_path_root = self.filepath
            except IndexError:
                self.report({'ERROR'}, f"Invalid section index: {idx}")
                return {'CANCELLED'}

        self.report({'INFO'},
                    f"Loaded {len(coords)} pts from {os.path.basename(self.filepath)}")
        _trigger_preview(self, context)
        return {'FINISHED'}


class WINGCREATOR_OT_clear_dat(Operator):
    bl_idname  = "wing_creator.clear_dat"
    bl_label   = "Clear .dat"
    bl_options = {'REGISTER', 'UNDO'}

    target: StringProperty(name="Target", default="CONSTANT")

    def execute(self, context):
        props = context.scene.wing_creator
        if self.target == 'CONSTANT':
            props.dat_path_constant = ""
        elif self.target == 'ROOT_TIP':
            props.dat_path_root_tip = ""
        else:
            parts = self.target.split(':')
            idx = int(parts[0])
            end = parts[1] if len(parts) > 1 else 'root'
            try:
                s = props.sections[idx]
                if end == 'tip':
                    s.dat_path_tip  = ""
                else:
                    s.dat_path_root = ""
            except IndexError:
                pass
        _trigger_preview(self, context)
        return {'FINISHED'}


class WINGCREATOR_OT_create(Operator):
    """Create OR rebuild the wing (reuses existing object if one is bound)."""
    bl_idname  = "wing_creator.create"
    bl_label   = "Create Wing"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.wing_creator

        # Reuse existing wing object rather than creating a duplicate
        wing = _ensure_wing_object(context, props)
        build_wing_mesh(props, wing.data)
        empty = create_or_update_empty(context, props, wing)
        save_to_obj(props, wing)
        # Also tag the empty so selection handler can find it
        empty[WING_TAG] = wing.name

        bpy.ops.object.select_all(action='DESELECT')
        empty.select_set(True)
        context.view_layer.objects.active = empty

        props.wing_created = True
        props.is_editing   = False
        _LAST_ACTIVE_NAME[0] = empty.name
        self.report({'INFO'}, f"Wing '{wing.name}' created.")
        return {'FINISHED'}


class WINGCREATOR_OT_edit(Operator):
    bl_idname = "wing_creator.edit"
    bl_label  = "Edit Wing"

    def execute(self, context):
        context.scene.wing_creator.is_editing = True
        return {'FINISHED'}


class WINGCREATOR_OT_update(Operator):
    bl_idname  = "wing_creator.update"
    bl_label   = "Update Wing"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.wing_creator
        wing  = _ensure_wing_object(context, props)
        build_wing_mesh(props, wing.data)
        empty = create_or_update_empty(context, props, wing)
        save_to_obj(props, wing)
        empty[WING_TAG] = wing.name
        props.wing_created = True
        props.is_editing   = False
        self.report({'INFO'}, "Wing updated.")
        return {'FINISHED'}


class WINGCREATOR_OT_check_updates(Operator):
    bl_idname = "wing_creator.check_updates"
    bl_label  = "Check for Updates"

    def execute(self, context):
        props = context.scene.wing_creator
        props.update_available = False
        props.latest_version   = ""
        props.latest_zip_url   = ""
        try:
            req = urllib.request.Request(
                GITHUB_API_URL,
                headers={"User-Agent": "WingCreator-Blender-Addon"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            self.report({'ERROR'}, f"Update check failed: {e}")
            return {'CANCELLED'}

        tag = data.get("tag_name", "").lstrip("v")
        try:
            remote = tuple(int(x) for x in tag.split("."))
        except ValueError:
            self.report({'WARNING'}, f"Cannot parse remote version: {tag}")
            return {'CANCELLED'}

        props.latest_version = tag
        for asset in data.get("assets", []):
            if asset.get("name", "").endswith(".zip"):
                props.latest_zip_url = asset["browser_download_url"]
                break

        local_str = ".".join(str(x) for x in ADDON_VERSION)
        if remote > ADDON_VERSION:
            props.update_available = True
            self.report({'INFO'}, f"Update available: v{tag}  (you have v{local_str})")
        else:
            self.report({'INFO'}, f"Up to date (v{local_str}).")
        return {'FINISHED'}


class WINGCREATOR_OT_install_update(Operator):
    bl_idname  = "wing_creator.install_update"
    bl_label   = "Install Update"
    bl_options = {'REGISTER'}

    def execute(self, context):
        props   = context.scene.wing_creator
        zip_url = props.latest_zip_url
        if not zip_url:
            self.report({'ERROR'}, "No URL — run Check for Updates first.")
            return {'CANCELLED'}
        try:
            req = urllib.request.Request(
                zip_url,
                headers={"User-Agent": "WingCreator-Blender-Addon"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                zip_data = resp.read()
        except Exception as e:
            self.report({'ERROR'}, f"Download failed: {e}")
            return {'CANCELLED'}
        tmp_dir  = tempfile.mkdtemp()
        zip_path = os.path.join(tmp_dir, "wing_creator_update.zip")
        try:
            with open(zip_path, 'wb') as f:
                f.write(zip_data)
            bpy.ops.preferences.addon_install(overwrite=True, filepath=zip_path)
            bpy.ops.preferences.addon_enable(module=__name__.split(".")[0])
        except Exception as e:
            self.report({'ERROR'}, f"Install failed: {e}")
            return {'CANCELLED'}
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        props.update_available = False
        self.report({'INFO'},
                    f"Updated to v{props.latest_version}. Restart recommended.")
        return {'FINISHED'}


class WINGCREATOR_OT_open_docs(Operator):
    bl_idname = "wing_creator.open_docs"
    bl_label  = "Documentation"

    def execute(self, context):
        import webbrowser
        webbrowser.open(GITHUB_REPO_URL)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Selection-tracking handler
# ---------------------------------------------------------------------------

@bpy.app.handlers.persistent
def _on_selection_change(scene, depsgraph):
    ctx    = bpy.context
    active = getattr(ctx, 'active_object', None)
    current = active.name if active else None
    if current == _LAST_ACTIVE_NAME[0]:
        return
    _LAST_ACTIVE_NAME[0] = current

    props = scene.wing_creator
    if active is None:
        reset_props(props)
        return

    # Primary path: active object is the wing's root empty
    wing_name = active.get(WING_TAG)
    if wing_name and wing_name in bpy.data.objects:
        wing = bpy.data.objects[wing_name]
        if load_from_obj(wing, props):
            return

    # Fallback: active object is the wing mesh itself (shouldn't happen with
    # hide_select but keep for robustness)
    if load_from_obj(active, props):
        return

    reset_props(props)


# ---------------------------------------------------------------------------
# N-Panel
# ---------------------------------------------------------------------------

class WINGCREATOR_PT_main(Panel):
    bl_label       = "Wing Creator"
    bl_idname      = "WINGCREATOR_PT_main"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Wing Creator"

    def draw(self, context):
        layout = self.layout
        props  = context.scene.wing_creator
        locked = props.wing_created and not props.is_editing
        mode   = props.chord_mode

        # ── HEADER ──────────────────────────────────────────────────────────
        box = layout.box()
        row = box.row()
        row.prop(props, "header_expanded",
                 icon='TRIA_DOWN' if props.header_expanded else 'TRIA_RIGHT',
                 icon_only=True, emboss=False)
        row.label(text="Wing Creator  v0.5.0", icon='MATFLUID')
        if props.header_expanded:
            row = box.row(align=True)
            row.operator("wing_creator.open_docs",     icon='URL',
                         text="Documentation")
            row.operator("wing_creator.check_updates", icon='FILE_REFRESH',
                         text="Check for Updates")
            if props.update_available:
                sub = box.column()
                sub.alert = True
                sub.label(text=f"Update available: v{props.latest_version}", icon='ERROR')
                sub.operator("wing_creator.install_update", icon='IMPORT',
                             text="Install Update Now")

        layout.separator()

        # ── WING-LEVEL SETTINGS ─────────────────────────────────────────────
        col = layout.column()
        col.enabled = not locked
        col.prop(props, "wing_name",  text="Wing Name")
        col.prop(props, "resolution", text="Resolution")
        col.prop(props, "use_mirror", text="Mirror Across X")
        if props.use_mirror:
            col.prop(props, "centerline_offset", text="Offset from Centerline")

        layout.separator()

        # ── CHORD TYPE ──────────────────────────────────────────────────────
        col = layout.column()
        col.enabled = not locked
        col.label(text="Chord Type:")
        col.prop(props, "chord_mode", expand=True)
        col.separator()

        if mode == 'CONSTANT':
            col.prop(props, "chord_constant", text="Chord")
            col.prop(props, "wingspan",        text="Wingspan")
            row = col.row(align=True)
            row.prop(props, "sweep_constant",    text="Sweep °")
            row.prop(props, "dihedral_constant", text="Dihedral °")
            col.separator()
            draw_airfoil_block(col, props,
                               "airfoil_constant", "dat_path_constant",
                               "CONSTANT", locked)

        elif mode == 'ROOT_TIP':
            row = col.row(align=True)
            row.prop(props, "chord_root", text="Root")
            row.prop(props, "chord_tip",  text="Tip")
            col.prop(props, "wingspan", text="Wingspan")
            row = col.row(align=True)
            row.prop(props, "sweep_root_tip",    text="Sweep °")
            row.prop(props, "dihedral_root_tip", text="Dihedral °")
            col.separator()
            draw_airfoil_block(col, props,
                               "airfoil_root_tip", "dat_path_root_tip",
                               "ROOT_TIP", locked)

        else:   # PER_SECTION
            col.label(text="Define each section below", icon='INFO')
            layout.separator()

            row = layout.row(align=True)
            row.enabled = not locked
            row.prop(props, "num_sections")
            row.operator("wing_creator.apply_sections", icon='CHECKMARK', text="")
            layout.separator()

            for i, section in enumerate(props.sections):
                self._draw_section(layout, props, section, i, locked)

        layout.separator()

        # ── PREVIEW ─────────────────────────────────────────────────────────
        icon = 'HIDE_OFF' if props.preview else 'HIDE_ON'
        layout.prop(props, "preview", text="Live Preview", icon=icon)
        if props.preview and not props.wing_object_name:
            layout.label(text="Press Create to initialize preview.", icon='INFO')

        layout.separator()

        # ── CREATE / EDIT / UPDATE ──────────────────────────────────────────
        if not props.wing_created:
            layout.operator("wing_creator.create", icon='MESH_DATA',    text="Create")
        elif props.is_editing:
            layout.operator("wing_creator.update", icon='FILE_REFRESH', text="Update")
        else:
            layout.operator("wing_creator.edit",   icon='GREASEPENCIL', text="Edit")

    # ── Per-section sub-panel ───────────────────────────────────────────────

    def _draw_section(self, layout, props, s, i: int, locked: bool):
        """Draw one section's collapsible panel."""
        box = layout.box()
        row = box.row()
        row.prop(s, "expanded",
                 icon='TRIA_DOWN' if s.expanded else 'TRIA_RIGHT',
                 icon_only=True, emboss=False)
        row.label(text=f"Section {i + 1}", icon='MOD_ARRAY')
        if not s.expanded:
            return

        col = box.column()
        col.enabled = not locked

        # ---- Sub-mode radio ----
        col.label(text="Section Chord:")
        col.prop(s, "section_chord_mode", expand=True)
        col.separator()

        # ---- Chord root (locked for sections after the first) ----
        chord_row = col.row()
        if i > 0:
            chord_row.enabled = False
            chord_row.prop(s, "chord_root", text="Root Chord (from prev)")
        else:
            chord_row.prop(s, "chord_root", text="Root Chord")

        # ---- Chord tip (only for ROOT_TIP sub-mode) ----
        if s.section_chord_mode == 'ROOT_TIP':
            col.prop(s, "chord_tip", text="Tip Chord")

        col.prop(s, "length", text="Section Length")

        # ---- Sweep / dihedral ----
        row2 = col.row(align=True)
        row2.prop(s, "sweep",    text="Sweep °")
        row2.prop(s, "dihedral", text="Dihedral °")

        col.separator()
        col.prop(s, "constant_airfoil", text="Constant Airfoil")

        if s.constant_airfoil:
            # Single airfoil for the section (root locked after first section
            # only when constant_airfoil was also True on previous section)
            af_locked = locked or (i > 0)
            draw_airfoil_block(col, s,
                               "airfoil_root", "dat_path_root",
                               f"{i}:root", af_locked,
                               label="Airfoil (NACA)")
        else:
            # Root airfoil — locked after section 0
            col.label(text="Beginning Airfoil:")
            af_locked = locked or (i > 0)
            draw_airfoil_block(col, s,
                               "airfoil_root", "dat_path_root",
                               f"{i}:root", af_locked,
                               label="Begin Airfoil (NACA)")
            col.separator()
            col.label(text="Ending Airfoil:")
            draw_airfoil_block(col, s,
                               "airfoil_tip", "dat_path_tip",
                               f"{i}:tip", locked,
                               label="End Airfoil (NACA)")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    WingSection,
    WingCreatorProperties,
    WINGCREATOR_OT_apply_sections,
    WINGCREATOR_OT_import_dat,
    WINGCREATOR_OT_clear_dat,
    WINGCREATOR_OT_create,
    WINGCREATOR_OT_edit,
    WINGCREATOR_OT_update,
    WINGCREATOR_OT_check_updates,
    WINGCREATOR_OT_install_update,
    WINGCREATOR_OT_open_docs,
    WINGCREATOR_PT_main,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.wing_creator = PointerProperty(type=WingCreatorProperties)
    if _on_selection_change not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_selection_change)


def unregister():
    if _on_selection_change in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_selection_change)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "wing_creator"):
        del bpy.types.Scene.wing_creator


if __name__ == "__main__":
    register()
