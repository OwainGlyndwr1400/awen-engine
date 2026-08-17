"""
build_regulus_corridor.py — precompute the Regulus corridor for the Awen Deck.

The deck's Sphinx panel must be an instrument, not a picture with numbers typed
next to it. So the star's declination is computed here, once, with astropy (the
same tool the Corridor paper used), and written to docs/regulus_corridor.json.
The panel then does only the cheap part in JS: the spherical triangle that turns
a declination and a latitude into an altitude and an azimuth.

    cos A = (sin d - sin h sin phi) / (cos h cos phi)      azimuth at altitude h
    h     = asin( sin d / sin phi )                        altitude at A = 90.00

Method, stated so the limits are visible on the panel itself:
  - ICRS J2000 -> FK5 mean equinox of date (astropy IAU2006 precession).
  - Proper motion applied linearly by hand: SkyCoord.apply_space_motion routes
    through TDB and trips ERFA's taiutc before ~-4700. Over 12,000 yr Regulus's
    pm moves its DECLINATION by ~0.019 deg, and declination is the only term
    that sets azimuth at a given altitude, so linear is well inside the
    precession model's own uncertainty at this range.
  - Geometric horizon. No refraction, no local horizon profile.
  - astropy's precession polynomial is being extrapolated well past its design
    span at -12,000. That is the dominant uncertainty and the panel says so.

Control stars are included deliberately. Precession carries every near-ecliptic
star through due east eventually; shipping the controls means the panel can
never quietly imply the crossing alone is the finding. See REGULUS_REVIEW.md.

Run:  py -3.11 build_regulus_corridor.py
"""

import json
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from astropy import units as u
from astropy.coordinates import FK5, SkyCoord
from astropy.time import Time

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "regulus_corridor.json"

YEAR_START, YEAR_END, YEAR_STEP = -12000, 2101, 25

# Hipparcos/Gaia positions, J2000. mag = apparent visual.
STARS = {
    "regulus": dict(
        label="Regulus  α Leo",
        gloss="Cor Leonis — the Lion's Heart. Brightest star in Leo; the only "
              "1st-magnitude star sitting on the ecliptic.",
        mag=1.35, ra=152.092962, dec=11.967209, pmra=-248.73, pmdec=5.59,
        primary=True,
    ),
    "algieba": dict(
        label="Algieba  γ Leo", gloss="Control star used in the Corridor paper.",
        mag=2.08, ra=154.993145, dec=19.841489, pmra=-310.20, pmdec=-152.90,
        primary=False,
    ),
    "denebola": dict(
        label="Denebola  β Leo", gloss="Control — the Lion's tail.",
        mag=2.11, ra=177.264910, dec=14.572058, pmra=-497.68, pmdec=-114.67,
        primary=False,
    ),
    "zosma": dict(
        label="Zosma  δ Leo", gloss="Control — the Lion's hip.",
        mag=2.56, ra=168.527079, dec=20.523717, pmra=143.42, pmdec=-129.88,
        primary=False,
    ),
    "epsleo": dict(
        label="ε Leo", gloss="Control — nearest competitor to Regulus by epoch, "
                             "and ~5x fainter.",
        mag=2.98, ra=146.462898, dec=23.774209, pmra=-46.30, pmdec=-9.60,
        primary=False,
    ),
}

SITES = [
    dict(id="giza", label="Great Sphinx · Giza", lat=29.9753, lon=31.1376,
         note="29.9792458°N at the plateau — the speed-of-light latitude."),
    dict(id="gowerton", label="Gowerton · Cymru", lat=51.6486, lon=-4.0361,
         note="Home ground. Same bearing, higher star — latitude is the only "
              "thing that changes."),
    dict(id="gobekli", label="Göbekli Tepe", lat=37.2231, lon=38.9225,
         note="Regulus due east at altitude 20° c. 9500 BCE."),
    dict(id="serpent", label="Serpent Mound", lat=39.0254, lon=-83.4302,
         note="Regulus sightline at azimuth 300° c. 3332 BCE."),
]

PAPERS = [
    dict(doi="10.5281/zenodo.19164088",
         title="Astronomical Alignment 2026: Regulus And The Great Sphinx"),
    dict(doi="10.5281/zenodo.19390874",
         title="The Regulus Corridor Through Time: 12,000 Years of Eastward "
               "Alignments at the Great Sphinx"),
    dict(doi="10.5281/zenodo.19423144",
         title="The Lion Watches The Lion: Regulus and the Eastern Horizon — "
               "A Cross-Site Analysis of Stellar Alignments at Ancient Monuments"),
]


def dec_series(star, years):
    """Declination at the mean equinox of each year, degrees."""
    out = []
    for y in years:
        dt = y - 2000.0
        dec0 = star["dec"] + (star["pmdec"] * dt) / 3.6e6
        ra0 = star["ra"] + (star["pmra"] * dt) / 3.6e6 / np.cos(np.radians(star["dec"]))
        t = Time(float(y), format="jyear", scale="tt")
        c = SkyCoord(ra=ra0 * u.deg, dec=dec0 * u.deg, frame="icrs")
        out.append(round(float(c.transform_to(FK5(equinox=t)).dec.deg), 5))
    return out


def altitude_at_due_east(dec, lat):
    """Altitude when the star sits at azimuth exactly 90.00 deg. None if it
    never does (|sin dec| > |sin lat|)."""
    s = np.sin(np.radians(dec)) / np.sin(np.radians(lat))
    return None if abs(s) > 1.0 else round(float(np.degrees(np.arcsin(s))), 4)


def main():
    years = list(range(YEAR_START, YEAR_END, YEAR_STEP))
    print(f"computing {len(years)} epochs x {len(STARS)} stars "
          f"({YEAR_START} .. {YEAR_END}, step {YEAR_STEP})")

    stars = {}
    for key, star in STARS.items():
        decs = dec_series(star, years)
        stars[key] = {
            "label": star["label"], "gloss": star["gloss"], "mag": star["mag"],
            "primary": star["primary"], "dec": decs,
        }
        # Where does it cross due east at the Sphinx, and how tightly?
        best = min(
            ((y, d) for y, d in zip(years, decs)),
            key=lambda yd: abs(altitude_at_due_east(yd[1], SITES[0]["lat"]) or 9e9),
        )
        print(f"  {star['label']:<18} mag {star['mag']:.2f}  "
              f"nearest due-east crossing at Giza: {abs(best[0]) if best[0] < 0 else best[0]}"
              f"{' BCE' if best[0] < 0 else ' CE'}  (dec {best[1]:+.4f})")

    # Present-day altitude at due east, per site — the Gowerton readout.
    now_idx = min(range(len(years)), key=lambda i: abs(years[i] - 2026))
    print("\n  present-day altitude of Regulus at azimuth 90.00 deg:")
    for s in SITES:
        h = altitude_at_due_east(stars["regulus"]["dec"][now_idx], s["lat"])
        s["alt_today"] = h
        print(f"    {s['label']:<24} lat {s['lat']:7.4f}  ->  alt {h:6.2f} deg")

    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "years": {"start": YEAR_START, "end": YEAR_END - 1, "step": YEAR_STEP,
                  "count": len(years)},
        "stars": stars,
        "sites": SITES,
        "papers": PAPERS,
        "method": (
            "astropy IAU2006 precession, ICRS J2000 -> FK5 mean equinox of date. "
            "Proper motion linear. Geometric horizon: no refraction, no local "
            "horizon profile. The precession polynomial is extrapolated beyond "
            "its design span before ~-4000; treat deep epochs as indicative."
        ),
        "honesty": (
            "Precession carries EVERY star with |ecliptic latitude| < obliquity "
            "through declination 0, and declination 0 is due east. So a crossing "
            "on its own is not evidence. The controls are shipped alongside so "
            "this panel cannot imply otherwise. What distinguishes Regulus is "
            "the conjunction: brightest star in Leo, named the Lion's Heart, at "
            "azimuth 90.00 from a lion-bodied monument, in that monument's epoch."
        ),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"\nwrote {OUT}  ({OUT.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
