# Wing Creator — Blender Add-on v0.1.0

A parametric wing generator for Blender 4.5+.  
Creates lofted wing geometry from NACA airfoil sections defined in the N-Panel.

---

## Installation

1. In Blender: **Edit → Preferences → Add-ons → Install**
2. Select `wing_creator_v0.1.0.zip`
3. Enable **Wing Creator** in the add-on list
4. Open the **N-Panel** (press `N` in the 3D Viewport) → **Wing Creator** tab

---

## Usage

### 1. Chord Type
Choose how chord is defined across the wing:
- **Constant Chord** — one chord value applies to all sections
- **Root & Tip Chord** — chord interpolates linearly from root to tip
- **Chord Per Section** — each section has its own chord value

### 2. Number of Sections
Enter the number of sections and press **✓** (Apply).  
This creates the repeating section panels below.

### 3. Section Panels
Each section is collapsible and contains:
- **Airfoil (NACA)** — 4, 5, or 6-digit NACA code (e.g. `2412`, `23012`, `631-412`)
- **Chord** — (visible when "Chord Per Section" is selected)
- **Section Length** — the spanwise length of this section

### 4. Create / Edit / Update
- **Create** — builds the wing mesh and locks all inputs
- **Edit** — unlocks inputs for changes
- **Update** — rebuilds the wing with the updated values

---

## Geometry Convention

| Direction | Meaning |
|-----------|---------|
| **+X** | Spanwise (root → tip) |
| **+Y** | Chordwise (leading edge → trailing edge) |
| **+Z** | Thickness (top surface) |

- Wing starts at the **world origin**
- Leading edge is at Y=0; trailing edge at Y=chord

---

## Supported Airfoil Codes

| Format | Example | Notes |
|--------|---------|-------|
| NACA 4-digit | `2412` | Full support |
| NACA 5-digit | `23012` | Standard camber lines (210–250 series) |
| NACA 6-digit | `631412` | Approximated via last 4 digits (v0.1) |

---

## Roadmap

- [ ] Full NACA 6-series generation
- [ ] Custom airfoil import (`.dat` files)
- [ ] Dihedral / sweep / twist per section
- [ ] Live Preview mode
- [ ] Export to IGES / STEP
- [ ] Auto-update via GitHub API

---

## Links

- **Documentation:** https://github.com/your-repo/wing-creator
- **Issues:** https://github.com/your-repo/wing-creator/issues

---

## License

MIT License — see LICENSE file.
