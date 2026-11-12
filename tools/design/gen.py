"""Generates the nine .dc.html artboards for the Yarbo super-user canvas (shared nav + CSS)."""
from pathlib import Path

OUT = Path(__file__).parent / "project"
OUT.mkdir(exist_ok=True)

FONT = ('<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500'
        '&amp;family=IBM+Plex+Sans:wght@400;500;600;700&amp;display=swap" rel="stylesheet">')

BASE_CSS = r"""
body{margin:0;background:#0E1311}
a{color:#9DD5A8}a:hover{color:#C4EBCB}
.app{min-height:100vh;display:flex;flex-wrap:wrap;background:#0E1311;color:#E6ECE8;font-family:'IBM Plex Sans',system-ui,sans-serif;font-size:14px;line-height:1.45}
.side{flex:0 0 220px;box-sizing:border-box;padding:20px 12px;border-right:1px solid #222C27;display:flex;flex-direction:column;gap:2px;background:#0B100E}
.brand{display:flex;align-items:center;gap:10px;padding:4px 12px 22px;font-weight:700;font-size:15px}
.nav{display:flex;align-items:center;gap:12px;min-height:44px;padding:0 12px;border-radius:8px;color:#B7C4BD;text-decoration:none;font-weight:500}
.nav:hover{background:#151C19;color:#E6ECE8}
.nav.on{background:#18231E;color:#E6ECE8}
.ico{width:18px;height:18px;flex:0 0 18px;fill:none;stroke:currentColor;stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round}
.main{flex:1 1 680px;min-width:0;display:flex;flex-direction:column}
.top{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:16px 24px;border-bottom:1px solid #222C27}
.h1{margin:0 auto 0 0;font-size:20px;font-weight:600}
.chip{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;background:#141B18;border:1px solid #2A3530;font-size:12.5px;color:#C9D4CE;white-space:nowrap}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;flex:0 0 8px}
.card{background:#141B18;border:1px solid #222C27;border-radius:12px;padding:16px;box-sizing:border-box}
.label{font-size:11.5px;letter-spacing:.6px;text-transform:uppercase;color:#93A29A;font-weight:600;margin:0 0 10px}
.mono{font-family:'IBM Plex Mono',ui-monospace,monospace}
.muted{color:#A3B1AA}
.big{font-size:26px;font-weight:700;letter-spacing:-.2px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;padding:0 16px;border-radius:9px;border:1px solid #2E3A34;background:#1A2420;color:#E6ECE8;font:inherit;font-weight:600;cursor:pointer;text-decoration:none}
.btn:hover{background:#21302A}
.btn.on{background:#22302A;border-color:#3A4A42}
.btn.pri{border-color:transparent;color:#0E1311}
.btn[disabled]{opacity:.55;cursor:not-allowed}
.seg{display:inline-flex;flex-wrap:wrap;gap:4px;padding:3px;border-radius:10px;background:#0E1311;border:1px solid #222C27}
.seg button{min-height:44px;padding:0 12px;border-radius:7px;border:0;background:transparent;color:#B7C4BD;font:inherit;font-size:13px;font-weight:500;cursor:pointer}
.seg button.on{background:#1E2924;color:#E6ECE8}
.li{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid #1E2723}
.kv{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px solid #1E2723}
.field{width:100%;box-sizing:border-box;min-height:44px;margin-top:6px;padding:0 12px;border-radius:9px;background:#0E1311;color:#E6ECE8;border:1px solid #2E3A34;font:inherit}
.tag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11.5px;font-weight:600;white-space:nowrap}
.bar{height:8px;border-radius:4px;background:#1E2924;overflow:hidden}
.fill{height:100%;border-radius:4px}
.ex{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11.5px;font-weight:600;background:#1D3446;color:#A9D2F5}
"""

AERIAL_CSS = r"""
.t0{fill:#2C4527}.t1{fill:#365330}.t2{fill:#25401F}.turfbg{fill:url(#turf)}
.road{fill:#2B2E2D}.roadline{stroke:#6E6A55;stroke-width:2;stroke-dasharray:14 12}
.concrete{fill:#6F716C}.roof{fill:#4B4743}.roofa{fill:#5C5751}.ridge{stroke:#34312D;stroke-width:2}.roofshadow{fill:#121C10;opacity:.65}
.patio{fill:#7A736A}.mulch{fill:#4D3B27}.sand{fill:#8C7B58}
.tree{fill:#1D391B}.treehl{fill:#2A4E26}.treeshadow{fill:#101E0F;opacity:.75}
.hbg{fill:#33260F}.hl{stroke:#F0B955;stroke-width:2.5}
.zone{fill:#8BD49A;fill-opacity:.10;stroke:#8BD49A;stroke-width:2.5}
.zonep{fill:none;stroke:#D8E0DC;stroke-width:2;stroke-dasharray:8 5}
.nogo{fill:url(#hatch);fill-opacity:.8;stroke:#F0B955;stroke-width:2.5}
.novis{fill:none;stroke:#79B8F0;stroke-width:2.5;stroke-dasharray:6 5}
.pathw{fill:none;stroke:#F2F5F3;stroke-width:3;stroke-dasharray:1 7;stroke-linecap:round}
.dock{fill:#0E1311;stroke:#8BD49A;stroke-width:2}
.robot{fill:#8BD49A;stroke:#0E1311;stroke-width:2.5}
.fov{fill:#79B8F0;fill-opacity:.07;stroke:#79B8F0;stroke-width:1.5;stroke-dasharray:4 4}
.fov.hot{fill-opacity:.16;stroke-dasharray:none}
.cam{fill:#0E1311;stroke:#79B8F0;stroke-width:2}
.lbl{fill:#F2F5F3;font-size:13px;font-family:'IBM Plex Sans',sans-serif;font-weight:600;paint-order:stroke;stroke:#0E1311;stroke-width:3px;stroke-linejoin:round}
.lbl.s{font-size:11.5px;font-weight:500}
.lbl.w{fill:#F7D38E}
.lbl.b{fill:#A9D2F5}
.c{text-anchor:middle}.r{text-anchor:end}
.attr{fill:#D3DDD8;font-size:10.5px;font-family:'IBM Plex Sans',sans-serif;paint-order:stroke;stroke:#0E1311;stroke-width:3px}
"""

ICONS = {
    "live": '<circle cx="12" cy="12" r="3"></circle><circle cx="12" cy="12" r="8"></circle><path d="M12 1v3M12 20v3M1 12h3M20 12h3"></path>',
    "runs": '<path d="M3 12a9 9 0 1 0 3-6.7"></path><path d="M3 4v5h5"></path><path d="M12 7v5l3 2"></path>',
    "map": '<path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2z"></path><path d="M9 4v14M15 6v14"></path>',
    "cal": '<rect x="3" y="5" width="18" height="16" rx="2"></rect><path d="M3 10h18M8 3v4M16 3v4"></path>',
    "chart": '<path d="M4 20V11M10 20V5M16 20v-6M22 20H2"></path>',
    "wrench": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z"></path>',
    "pulse": '<path d="M3 12h4l3-7 4 14 3-7h4"></path>',
    "flask": '<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3"></path><path d="M7.5 15h9"></path>',
    "gear": '<circle cx="12" cy="12" r="3.5"></circle><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"></path>',
}
NAV = [("Main", "Live", "live"), ("Runs", "Runs", "runs"), ("MapStudio", "Map Studio", "map"),
       ("Schedule", "Schedule", "cal"), ("Insights", "Insights", "chart"), ("Maintenance", "Maintenance", "wrench"),
       ("Health", "Health", "pulse"), ("Lab", "Lab", "flask"), ("Settings", "Settings", "gear")]


def nav(active):
    out = ['<nav class="side" aria-label="Sections">',
           '<div class="brand"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true" style="width:22px;height:22px;flex-basis:22px">'
           '<rect x="3" y="7" width="18" height="10" rx="3"></rect><circle cx="7" cy="18" r="2"></circle><circle cx="17" cy="18" r="2"></circle>'
           '<path d="M8 7V5h8v2"></path></svg><span>Yarbo <span style="color:#93A29A;font-weight:500">super-user</span></span></div>']
    for fname, label, icon in NAV:
        on = fname == active
        cls = "nav on" if on else "nav"
        cur = ' aria-current="page"' if on else ""
        sty = ' style="color: {{accent}}"' if on else ""
        out.append(f'<a href="{fname}.dc.html" class="{cls}"{cur}><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"{sty}>{ICONS[icon]}</svg>{label}</a>')
    out.append('<div class="card" style="margin-top:auto;padding:12px"><p class="label" style="margin-bottom:6px">Robot</p>'
               '<div class="mono" style="font-size:12px">YB0000DEMO0001</div><div class="muted" style="font-size:12.5px">Core · Lawn Mower Pro</div>'
               '<div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap"><span class="chip">Super-user on</span></div></div>')
    out.append("</nav>")
    return "\n".join(out)


HATCH = ('<pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
         '<rect width="8" height="8" class="hbg"></rect><line x1="0" y1="0" x2="0" y2="8" class="hl"></line></pattern>')
YARD = "60,196 300,176 530,170 704,206 744,420 430,462 92,440"
SIDE = "540,36 730,52 742,178 556,160"
DRIVE = "120,26 296,26 296,150 150,176"


def aerial(overlay, defs="", aria="Aerial-style map of the yard"):
    return f"""<svg viewBox="0 0 800 480" style="display:block;width:100%;height:auto" role="img" aria-label="{aria}">
<defs>
<pattern id="turf" width="14" height="14" patternUnits="userSpaceOnUse"><rect width="14" height="14" class="t0"></rect><circle cx="3" cy="4" r="1.3" class="t1"></circle><circle cx="10" cy="9" r="1.5" class="t2"></circle><circle cx="7" cy="12" r="1" class="t1"></circle></pattern>
{HATCH}
{defs}
</defs>
<rect width="800" height="480" class="turfbg"></rect>
<rect x="0" y="0" width="800" height="26" class="road"></rect>
<line x1="0" y1="13" x2="800" y2="13" class="roadline"></line>
<polygon points="{DRIVE}" class="concrete"></polygon>
<rect x="302" y="40" width="190" height="106" class="roofshadow"></rect>
<rect x="296" y="34" width="190" height="106" class="roof"></rect>
<polygon points="296,34 486,34 441,87 341,87" class="roofa"></polygon>
<line x1="341" y1="87" x2="441" y2="87" class="ridge"></line>
<rect x="388" y="140" width="14" height="36" class="concrete"></rect>
<rect x="596" y="370" width="76" height="64" rx="4" class="patio"></rect>
<polygon points="180,282 262,272 270,332 190,340" class="mulch"></polygon>
<rect x="250" y="384" width="92" height="44" rx="6" class="sand"></rect>
<circle cx="508" cy="338" r="44" class="treeshadow"></circle>
<circle cx="500" cy="330" r="42" class="tree"></circle>
<circle cx="487" cy="316" r="18" class="treehl"></circle>
<circle cx="62" cy="116" r="30" class="treeshadow"></circle>
<circle cx="56" cy="110" r="30" class="tree"></circle>
<circle cx="47" cy="101" r="12" class="treehl"></circle>
<circle cx="770" cy="300" r="26" class="tree"></circle>
<circle cx="762" cy="292" r="10" class="treehl"></circle>
<circle cx="766" cy="462" r="30" class="tree"></circle>
<circle cx="22" cy="320" r="24" class="tree"></circle>
{overlay}
<text x="790" y="472" class="attr r">Imagery: Esri World Imagery (illustrated here)</text>
</svg>"""


ZONES = f"""<polygon points="{SIDE}" class="zone"></polygon>
<polygon points="{YARD}" class="zone"></polygon>
<polygon points="{DRIVE}" class="zonep"></polygon>
<polygon points="180,282 262,272 270,332 190,340" class="nogo"></polygon>
<circle cx="500" cy="330" r="34" class="nogo"></circle>
<polygon points="600,382 660,374 668,422 610,430" class="nogo"></polygon>
<rect x="250" y="384" width="92" height="44" class="novis"></rect>
<polyline points="150,176 160,198" class="pathw"></polyline>
<polyline points="296,140 300,178" class="pathw"></polyline>
<polyline points="530,170 556,160" class="pathw"></polyline>
<rect x="96" y="204" width="24" height="24" rx="4" class="dock"></rect>"""

LABELS = """<text x="391" y="112" class="lbl c">House</text>
<text x="210" y="70" class="lbl c">upper driveway</text>
<text x="210" y="86" class="lbl s c">181 m²</text>
<text x="642" y="104" class="lbl c">side yard</text>
<text x="642" y="120" class="lbl s c">78 m²</text>
<text x="380" y="252" class="lbl c">All Yard</text>
<text x="380" y="268" class="lbl s c">1,743 m²</text>"""

CAMS = """<polygon points="300,146 70,440 400,466" class="fov"></polygon>
<polygon points="486,146 470,466 770,410" class="fov"></polygon>
<polygon points="296,96 52,124 76,272" class="fov"></polygon>
<circle cx="300" cy="146" r="7" class="cam"></circle><text x="292" y="168" class="lbl s b r">back_yard</text>
<circle cx="486" cy="146" r="7" class="cam"></circle><text x="496" y="168" class="lbl s b">back_deck</text>
<circle cx="296" cy="96" r="7" class="cam"></circle><text x="286" y="122" class="lbl s b r">garage</text>"""

LEGEND = """<div style="display:flex;flex-wrap:wrap;gap:18px;padding:12px 14px;border-top:1px solid #222C27;font-size:12.5px;color:#C9D4CE">
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:14px;height:10px;background:#2C4527;border:2px solid #8BD49A;border-radius:2px"></span>Mowing area</span>
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:14px;height:10px;background:#33260F;border:2px solid #F0B955;border-radius:2px"></span>No-go zone</span>
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:14px;height:10px;border:2px dashed #79B8F0;border-radius:2px"></span>No-vision zone</span>
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:16px;border-top:3px dotted #F2F5F3"></span>Pathway</span>
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:14px;height:10px;background:rgba(121,184,240,.16);border:1.5px dashed #79B8F0;border-radius:2px"></span>Camera coverage</span>
</div>"""


def page(name, title, extra_css, top, body, js, height):
    props = ('{"accent":{"editor":"color","default":"#8BD49A","options":["#8BD49A","#79B8F0","#F0B955","#E6ECE8"]},'
             f'"$preview":{{"width":1440,"height":{height}}}}}')
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Yarbo super-user: {title}</title>
<script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
{FONT}
<style>{BASE_CSS}{extra_css}</style>
</helmet>
<div class="app">
{nav(name)}
<div class="main">
<header class="top">
{top}
</header>
{body}
</div>
</div>
</x-dc>
<script type="text/x-dc" data-dc-script data-props='{props}'>
class Component extends DCLogic {{
  renderVals() {{
    const accent = this.props.accent ?? '#8BD49A';
{js}
  }}
}}
</script>
</body>
</html>
"""
    (OUT / f"{name}.dc.html").write_text(html, encoding="utf-8")


def chip(text, color=None):
    d = f'<span class="dot" style="background:{color}"></span>' if color else ""
    return f'<span class="chip">{d}{text}</span>'


# ---------------------------------------------------------------- Live
CV = """<svg viewBox="0 0 640 360" style="display:block;width:100%;height:auto;border-radius:9px" role="img" aria-label="Example camera frame: a backyard lawn with the robot outlined by a robot_lawnmower detection box">
<rect width="640" height="360" style="fill:#1A2818"></rect>
<polygon points="0,124 640,100 640,360 0,360" style="fill:#2F4A2A"></polygon>
<line x1="60" y1="360" x2="250" y2="122" style="stroke:#36552F;stroke-width:26"></line>
<line x1="230" y1="360" x2="330" y2="116" style="stroke:#36552F;stroke-width:26"></line>
<line x1="400" y1="360" x2="410" y2="110" style="stroke:#36552F;stroke-width:26"></line>
<line x1="570" y1="360" x2="490" y2="106" style="stroke:#36552F;stroke-width:26"></line>
<polygon points="0,124 640,100 640,84 0,108" style="fill:#5A4A3A"></polygon>
<rect x="500" y="40" width="16" height="70" style="fill:#3B2E22"></rect>
<circle cx="508" cy="44" r="62" style="fill:#1D391B"></circle>
<rect x="270" y="222" width="96" height="46" rx="10" style="fill:#D9DDD8"></rect>
<rect x="282" y="210" width="72" height="16" rx="5" style="fill:#3A3F3C"></rect>
<circle cx="284" cy="270" r="11" style="fill:#151815"></circle><circle cx="352" cy="270" r="11" style="fill:#151815"></circle>
<rect x="258" y="198" width="120" height="86" style="fill:none;stroke:#8BD49A;stroke-width:2.5"></rect>
<rect x="258" y="178" width="132" height="20" style="fill:#8BD49A"></rect>
<text x="264" y="193" style="fill:#0E1311;font-size:13px;font-family:'IBM Plex Mono',monospace;font-weight:500">robot_lawnmower</text>
<circle cx="20" cy="20" r="5" style="fill:#F08A6E"></circle>
<text x="32" y="25" style="fill:#F2F5F3;font-size:14px;font-family:'IBM Plex Mono',monospace">back_yard</text>
<text x="626" y="25" style="fill:#F2F5F3;font-size:14px;font-family:'IBM Plex Mono',monospace;text-anchor:end">live</text>
</svg>"""

page("Main", "Live", AERIAL_CSS,
     '<h1 class="h1">Live</h1>' + chip("Docked · charging", "#8BD49A") + chip('Battery <b class="mono">95%</b>')
     + chip("RTK no fix", "#F0B955") + chip("Lawn: wet", "#79B8F0") + chip("Cloud session", "#8BD49A"),
     f"""<div style="display:flex;flex-wrap:wrap;gap:16px;padding:20px 24px;align-items:flex-start">
<div style="flex:1 1 600px;min-width:0;display:flex;flex-direction:column;gap:16px">
<section class="card" style="padding:0;overflow:hidden" aria-label="Yard map">
<div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:10px 12px;border-bottom:1px solid #222C27">
<div class="seg" role="group" aria-label="Map layers">
<button class="on" type="button" aria-pressed="true">Zones</button>
<button type="button" aria-pressed="false">Last track</button>
<button type="button" aria-pressed="false">RTK quality</button>
<button type="button" aria-pressed="false">Trouble spots</button>
<button class="on" type="button" aria-pressed="true">Cameras</button>
</div>
<span class="muted" style="margin-left:auto;font-size:12.5px">Yard layout is illustrative</span>
</div>
{aerial(ZONES + CAMS + '<circle cx="108" cy="216" r="7" class="robot"></circle>' + LABELS, aria="Aerial map with mowing areas, no-go zones, three camera coverage cones, and the robot at its dock")}
{LEGEND}
</section>
<section class="card" aria-label="Follow-cam">
<div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:12px">
<p class="label" style="margin:0 auto 0 0">Follow-cam</p>
<span class="ex">Example frame</span>
<span class="chip"><span class="dot" style="background:#8BD49A"></span>Auto: follows the robot</span>
</div>
<div style="display:flex;flex-wrap:wrap;gap:16px">
<div style="flex:1 1 380px;min-width:0">{CV}</div>
<div style="flex:0 1 220px;min-width:200px;display:flex;flex-direction:column;gap:8px">
<p class="muted" style="margin:0;font-size:12.5px">Switches to whichever yard camera covers the robot's position. Coverage is learned during one calibration mow; <span class="mono">robot_lawnmower</span> detections confirm it.</p>
<sc-for list="{{{{cams}}}}" as="c" hint-placeholder-count="6">
<button class="btn" type="button" style="justify-content:space-between;background: {{{{c.bg}}}}"><span class="mono" style="font-size:12.5px">{{{{c.name}}}}</span><span class="muted" style="font-size:12px;font-weight:500">{{{{c.state}}}}</span></button>
</sc-for>
</div>
</div>
</section>
</div>
<aside style="flex:0 1 380px;min-width:300px;display:flex;flex-direction:column;gap:16px" aria-label="Status, conditions and controls">
<section class="card">
<p class="label">Robot</p>
<div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap"><span class="big">Docked</span><span class="muted">charging · 95%</span></div>
<div class="muted" style="margin-top:2px">No plan running · kept awake by the phone app</div>
<dl style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 16px;margin:14px 0 0">
<div><dt class="muted" style="font-size:12px">Head</dt><dd style="margin:0;font-weight:600">Lawn Mower Pro</dd></div>
<div><dt class="muted" style="font-size:12px">Errors</dt><dd style="margin:0;font-weight:600">None</dd></div>
<div><dt class="muted" style="font-size:12px">Satellites</dt><dd class="mono" style="margin:0">26</dd></div>
<div><dt class="muted" style="font-size:12px">HaLow link</dt><dd class="mono" style="margin:0">-57 dBm</dd></div>
</dl>
</section>
<section class="card">
<div style="display:flex;align-items:center;gap:8px"><p class="label" style="margin:0 auto 0 0">Lawn conditions</p><span class="chip">Tempest + robot</span></div>
<div style="display:flex;align-items:baseline;gap:12px;margin-top:8px"><span class="big" style="color:#79B8F0">Wet</span><span class="muted">dry by [after sunrise + drying time]</span></div>
<div class="kv" style="margin-top:8px"><span class="muted">Rain today</span><span class="mono">0.27 in over 7 h 27 m</span></div>
<div class="kv"><span class="muted">Raining now</span><span class="mono">no</span></div>
<div class="kv"><span class="muted">Humidity · dew point</span><span class="mono">98% · 63.7 °F</span></div>
<div class="kv"><span class="muted">Sun</span><span class="mono">0 W/m²</span></div>
<div class="kv"><span class="muted">Robot rain sensor</span><span class="mono">calibrate</span></div>
</section>
<section class="card">
<p class="label">Start a plan</p>
<label for="plan" class="muted" style="font-size:12.5px">Plan</label>
<select id="plan" class="field"><option>Mow All</option><option>Mow Side Yard</option></select>
<ul style="list-style:none;margin:12px 0;padding:0" aria-label="Start checks">
<sc-for list="{{{{checks}}}}" as="c" hint-placeholder-count="7">
<li class="li"><span class="dot" style="background: {{{{c.color}}}}"></span><span style="flex:1">{{{{c.name}}}}</span><span class="mono muted" style="font-size:12.5px">{{{{c.value}}}}</span></li>
</sc-for>
</ul>
<button class="btn pri" type="button" disabled="{{{{yes}}}}" style="width:100%;background: {{{{accent}}}}">Start plan: blocked</button>
<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:8px">
<button class="btn" type="button">Pause</button><button class="btn" type="button">Resume</button>
<button class="btn" type="button">Stop</button><button class="btn" type="button">Return to dock</button>
</div>
</section>
</aside>
</div>""",
     """    return {
      accent, yes: true,
      cams: [
        { name: 'back_yard', state: 'showing', bg: '#22302A' },
        { name: 'back_deck', state: 'standby', bg: '#1A2420' },
        { name: 'garage', state: 'standby', bg: '#1A2420' },
        { name: 'front', state: 'standby', bg: '#1A2420' }
      ],
      checks: [
        { name: 'Online (local + cloud)', value: 'OK', color: '#8BD49A' },
        { name: 'No active errors', value: 'OK', color: '#8BD49A' },
        { name: 'Battery at or above plan start', value: '95%', color: '#8BD49A' },
        { name: 'RTK fix', value: 'none at dock', color: '#F0B955' },
        { name: 'Lawn dry', value: 'wet', color: '#F0B955' },
        { name: 'Yard clear (Frigate)', value: '[last detection]', color: '#79B8F0' },
        { name: 'Someone home (Alarmo)', value: '[state]', color: '#79B8F0' }
      ]
    };""", 1120)

# ---------------------------------------------------------------- Runs
STRIPES = "".join(f'<line x1="40" y1="{y}" x2="760" y2="{y}" class="tg"></line>' for y in range(190, 470, 22))
RUN_OVERLAY = (f'<polygon points="{SIDE}" class="zone"></polygon><polygon points="{YARD}" class="zone"></polygon>'
               f'<g class="clip">{STRIPES}'
               '<line x1="260" y1="190" x2="460" y2="190" class="tm"></line><line x1="300" y1="212" x2="420" y2="212" class="tm"></line>'
               '<line x1="440" y1="300" x2="560" y2="300" class="tm"></line><line x1="450" y1="322" x2="550" y2="322" class="tb"></line>'
               '<line x1="456" y1="344" x2="548" y2="344" class="tb"></line><line x1="446" y1="366" x2="560" y2="366" class="tm"></line>'
               '<rect x="380" y="420" width="60" height="30" class="gapcut"></rect><rect x="650" y="232" width="44" height="34" class="gapcut"></rect>'
               '</g>'
               '<polygon points="180,282 262,272 270,332 190,340" class="nogo"></polygon><circle cx="500" cy="330" r="34" class="nogo"></circle>'
               '<polygon points="600,382 660,374 668,422 610,430" class="nogo"></polygon>'
               '<rect x="380" y="420" width="60" height="30" class="gap"></rect><text x="410" y="414" class="lbl s c" style="fill:#F5B3A2">gap 1</text>'
               '<rect x="650" y="232" width="44" height="34" class="gap"></rect><text x="672" y="226" class="lbl s c" style="fill:#F5B3A2">gap 2</text>'
               '<rect x="96" y="204" width="24" height="24" rx="4" class="dock"></rect>')
RUN_CSS = AERIAL_CSS + r"""
.clip{clip-path:url(#yard)}
.tg{stroke:#8BD49A;stroke-width:12;stroke-opacity:.75}.tm{stroke:#F0B955;stroke-width:12}.tb{stroke:#F08A6E;stroke-width:12}
.gapcut{fill:#2C4527}
.gap{fill:#F08A6E;fill-opacity:.12;stroke:#F08A6E;stroke-width:2;stroke-dasharray:5 4}
.run{display:flex;flex-direction:column;gap:2px;width:100%;min-height:44px;text-align:left;padding:10px 12px;border-radius:9px;border:1px solid transparent;color:#E6ECE8;font:inherit;cursor:pointer}
"""
page("Runs", "Runs", RUN_CSS,
     '<h1 class="h1">Runs</h1>' + chip("Recorder: waiting for the first run") + chip("Gap detection on"),
     f"""<div style="display:flex;flex-wrap:wrap;gap:16px;padding:20px 24px;align-items:flex-start">
<section class="card" style="flex:0 1 330px;min-width:280px" aria-label="Run list">
<div class="seg" role="group" aria-label="Filter by plan" style="margin-bottom:12px">
<button class="on" type="button" aria-pressed="true">All</button><button type="button" aria-pressed="false">Mow All</button><button type="button" aria-pressed="false">Mow Side Yard</button>
</div>
<p class="muted" style="margin:0 0 10px;font-size:12.5px">Example rows. Real runs appear once the recorder has seen one.</p>
<div style="display:flex;flex-direction:column;gap:4px">
<sc-for list="{{{{runs}}}}" as="r" hint-placeholder-count="6">
<button class="run" type="button" style="background: {{{{r.bg}}}};border-color: {{{{r.bd}}}}">
<span style="display:flex;justify-content:space-between;gap:8px"><span style="font-weight:600">{{{{r.plan}}}}</span><span class="mono muted" style="font-size:12.5px">{{{{r.date}}}}</span></span>
<span style="display:flex;align-items:center;gap:8px;font-size:12.5px"><span class="dot" style="background: {{{{r.color}}}}"></span><span>{{{{r.end}}}}</span><span class="mono muted" style="margin-left:auto">{{{{r.dur}}}}</span></span>
</button>
</sc-for>
</div>
</section>
<div style="flex:1 1 560px;min-width:0;display:flex;flex-direction:column;gap:16px">
<section class="card" style="display:flex;flex-wrap:wrap;align-items:center;gap:12px">
<div style="margin-right:auto"><div style="font-size:20px;font-weight:700">Mow All</div><div class="muted mono" style="font-size:12.5px">[date] · [start] to [end]</div></div>
<span class="chip"><span class="dot" style="background:#8BD49A"></span>Completed</span><span class="chip">Lawn Mower Pro</span>
<button class="btn" type="button">Replay</button><button class="btn" type="button">Export GPX</button>
</section>
<div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px">
<div class="card"><p class="label" style="margin-bottom:4px">Duration</p><div class="mono" style="font-size:22px">[—]</div></div>
<div class="card"><p class="label" style="margin-bottom:4px">Covered</p><div class="mono" style="font-size:22px">[—]</div><div class="muted" style="font-size:12px">of 1,743 m² (All Yard)</div></div>
<div class="card"><p class="label" style="margin-bottom:4px">Energy</p><div class="mono" style="font-size:22px">[—]</div><div class="muted" style="font-size:12px">% per 100 m²</div></div>
<div class="card"><p class="label" style="margin-bottom:4px">Gaps</p><div class="mono" style="font-size:22px">[—]</div><div class="muted" style="font-size:12px">missed patches over 1 m²</div></div>
</div>
<section class="card" style="padding:0;overflow:hidden" aria-label="Track replay">
<div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:10px 12px;border-bottom:1px solid #222C27">
<div class="seg" role="group" aria-label="Track layer">
<button type="button" aria-pressed="false">Coverage</button><button class="on" type="button" aria-pressed="true">RTK</button>
<button type="button" aria-pressed="false">Satellites</button><button type="button" aria-pressed="false">HaLow</button>
<button type="button" aria-pressed="false">Battery</button><button type="button" aria-pressed="false">Blade load</button>
<button type="button" aria-pressed="false">Slope</button><button type="button" aria-pressed="false">Wheel slip</button>
</div>
</div>
{aerial(RUN_OVERLAY, defs=f'<clipPath id="yard"><polygon points="{YARD}"></polygon></clipPath>', aria="Example track over All Yard coloured by RTK quality, with two missed patches outlined")}
<div style="display:flex;flex-wrap:wrap;gap:18px;padding:12px 14px;border-top:1px solid #222C27;font-size:12.5px;color:#C9D4CE">
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:16px;height:8px;background:#8BD49A;border-radius:2px"></span>RTK fixed</span>
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:16px;height:8px;background:#F0B955;border-radius:2px"></span>RTK float</span>
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:16px;height:8px;background:#F08A6E;border-radius:2px"></span>Single point</span>
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:14px;height:10px;border:2px dashed #F08A6E;border-radius:2px"></span>Missed patch</span>
<span class="ex" style="margin-left:auto">Example track</span>
</div>
<div style="padding:14px 16px 18px;border-top:1px solid #222C27">
<p class="label">Timeline</p>
<div style="position:relative;height:8px;border-radius:4px;background:#1E2924;margin:18px 0 34px">
<div style="position:absolute;left:0;top:0;bottom:0;width:62%;border-radius:4px;background: {{{{accent}}}}"></div>
<sc-for list="{{{{marks}}}}" as="m" hint-placeholder-count="5">
<div style="position:absolute;top:-6px;left: {{{{m.left}}}};width:2px;height:20px;background: {{{{m.color}}}}"></div>
<div class="muted" style="position:absolute;top:18px;left: {{{{m.left}}}};transform:translateX(-50%);font-size:11.5px;white-space:nowrap">{{{{m.label}}}}</div>
</sc-for>
</div>
</div>
</section>
<section class="card" aria-label="Gap touch-ups">
<div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:8px"><p class="label" style="margin:0 auto 0 0">Gap touch-ups</p><span class="ex">Example</span></div>
<div class="li" style="border-top:0"><span class="dot" style="background:#F08A6E"></span><span style="flex:1"><b>Gap 1</b> · [area] m² near the play area</span><span class="muted" style="font-size:12.5px">likely: RTK float</span></div>
<div class="li"><span class="dot" style="background:#F08A6E"></span><span style="flex:1"><b>Gap 2</b> · [area] m² by the east fence</span><span class="muted" style="font-size:12.5px">likely: boundary margin</span></div>
<div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:12px">
<button class="btn pri" type="button" disabled="{{{{yes}}}}" style="background: {{{{accent}}}}">Touch up both gaps</button>
<button class="btn" type="button">Notify me instead</button>
<span class="muted" style="font-size:12.5px">Needs capture C7 (spot/waypoint mow). Obeys every safety and lawn-condition gate.</span>
</div>
</section>
</div>
</div>""",
     """    const sel = { bg: '#18231E', bd: '#2E3A34' };
    const rest = { bg: 'transparent', bd: 'transparent' };
    return {
      accent, yes: true,
      runs: [
        { plan: 'Mow All', date: '[date]', end: 'Completed · 2 gaps', dur: '[duration]', color: '#8BD49A', ...sel },
        { plan: 'Mow Side Yard', date: '[date]', end: 'Completed', dur: '[duration]', color: '#8BD49A', ...rest },
        { plan: 'Mow All', date: '[date]', end: 'Paused: low battery, resumed', dur: '[duration]', color: '#F0B955', ...rest },
        { plan: 'Mow All', date: '[date]', end: 'Skipped: lawn wet', dur: '—', color: '#79B8F0', ...rest },
        { plan: 'Mow All', date: '[date]', end: 'Error WP013: location data', dur: '[duration]', color: '#F08A6E', ...rest },
        { plan: 'Gap touch-up', date: '[date]', end: 'Completed · 1 gap', dur: '[duration]', color: '#8BD49A', ...rest }
      ],
      marks: [
        { left: '1%', label: 'Start', color: '#E6ECE8' },
        { left: '22%', label: 'RTK float', color: '#F0B955' },
        { left: '41%', label: 'Obstacle', color: '#F08A6E' },
        { left: '62%', label: 'Now', color: '#E6ECE8' },
        { left: '98%', label: 'End', color: '#A3B1AA' }
      ]
    };""", 1200)

# ---------------------------------------------------------------- Map Studio
MS_CSS = AERIAL_CSS + r"""
.sel{fill:url(#hatch);fill-opacity:.85;stroke:#F2F5F3;stroke-width:3}
.ghost{fill:none;stroke:#79B8F0;stroke-width:2.5;stroke-dasharray:6 4}
.handle{fill:#F2F5F3;stroke:#0E1311;stroke-width:1.5}.handle.new{fill:#79B8F0}
.meas{stroke:#F0B955;stroke-width:1.5;stroke-dasharray:3 3}
.tog{display:flex;align-items:center;gap:10px;width:100%;min-height:44px;padding:0 4px;border:0;border-top:1px solid #1E2723;background:transparent;color:#E6ECE8;font:inherit;text-align:left;cursor:pointer}
.box{width:16px;height:16px;border-radius:4px;border:1.5px solid #5F6E67;flex:0 0 16px;box-sizing:border-box}
.num{display:flex;flex-direction:column;gap:4px;font-size:12.5px;color:#A3B1AA}
"""
MS_OVERLAY = (ZONES.replace('<polygon points="180,282 262,272 270,332 190,340" class="nogo"></polygon>',
                            '<polygon points="180,282 262,272 270,332 190,340" class="sel"></polygon>'
                            '<polygon points="180,282 262,272 292,306 270,340 190,340" class="ghost"></polygon>')
              + CAMS
              + '<rect x="175" y="277" width="10" height="10" class="handle"></rect><rect x="257" y="267" width="10" height="10" class="handle"></rect>'
              '<rect x="265" y="335" width="10" height="10" class="handle"></rect><rect x="185" y="335" width="10" height="10" class="handle"></rect>'
              '<rect x="287" y="301" width="10" height="10" class="handle new"></rect>'
              '<line x1="270" y1="356" x2="190" y2="356" class="meas"></line><text x="230" y="374" class="lbl s c">8.1 m (example)</text>'
              '<text x="226" y="262" class="lbl w c">No-go Zone 3</text>' + LABELS)
page("MapStudio", "Map Studio", MS_CSS,
     '<h1 class="h1">Map Studio</h1>' + chip("Map v1 · fetched 2026-09-30") + chip("Draft: 1 change", "#79B8F0") + chip("Imagery not aligned yet", "#F0B955"),
     f"""<div role="toolbar" aria-label="Map tools" style="display:flex;flex-wrap:wrap;gap:8px;padding:12px 24px;border-bottom:1px solid #222C27">
<button class="btn on" type="button" aria-pressed="true">Select</button><button class="btn" type="button" aria-pressed="false">Edit boundary</button>
<button class="btn" type="button" aria-pressed="false">No-go zone</button><button class="btn" type="button" aria-pressed="false">Pathway</button>
<button class="btn" type="button" aria-pressed="false">Measure</button><button class="btn" type="button" aria-pressed="false">Align imagery</button>
<span style="flex:1"></span><button class="btn" type="button">Import GeoJSON / KML</button><button class="btn" type="button">Export</button>
</div>
<div style="display:flex;flex-wrap:wrap;gap:16px;padding:20px 24px;align-items:flex-start">
<section class="card" style="flex:1 1 560px;min-width:0;padding:0;overflow:hidden" aria-label="Map editor">
{aerial(MS_OVERLAY, aria="Aerial map with the garden-bed no-go zone selected, a proposed larger outline dashed in blue, and three camera coverage cones")}
<div style="display:flex;flex-wrap:wrap;gap:18px;padding:12px 14px;border-top:1px solid #222C27;font-size:12.5px;color:#C9D4CE">
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:14px;height:10px;border:2px solid #F2F5F3;border-radius:2px"></span>Selected (on robot)</span>
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:14px;height:10px;border:2px dashed #79B8F0;border-radius:2px"></span>Proposed (draft)</span>
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:10px;height:10px;background:#79B8F0;border:1.5px solid #0E1311"></span>New vertex</span>
<span style="display:inline-flex;align-items:center;gap:8px"><span style="width:14px;height:10px;background:rgba(121,184,240,.16);border:1.5px dashed #79B8F0;border-radius:2px"></span>Camera coverage (learned)</span>
</div>
</section>
<aside style="flex:0 1 380px;min-width:300px;display:flex;flex-direction:column;gap:16px" aria-label="Imagery, layers, inspector and write">
<section class="card">
<p class="label">Imagery</p>
<label for="src" class="muted" style="font-size:12.5px">Source</label>
<select id="src" class="field"><option>Esri World Imagery</option><option>Mapbox satellite (your key)</option><option>USGS NAIP</option><option>County orthophoto (WMS)</option><option>Upload GeoTIFF / drone map</option></select>
<label for="op" class="muted" style="font-size:12.5px;display:block;margin-top:12px">Opacity</label>
<input id="op" type="range" min="0" max="100" defaultValue="80" style="width:100%;min-height:32px;accent-color:#8BD49A">
<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:8px">
<label class="num">Shift east<input class="field mono" type="text" defaultValue="0.0 m" style="margin-top:0"></label>
<label class="num">Shift north<input class="field mono" type="text" defaultValue="0.0 m" style="margin-top:0"></label>
<label class="num">Rotate<input class="field mono" type="text" defaultValue="0.0°" style="margin-top:0"></label>
</div>
<button class="btn" type="button" style="width:100%;margin-top:10px">2-point align</button>
<p class="muted" style="margin:8px 0 0;font-size:12.5px">Imagery is often 1–3 m off; RTK is centimetre-accurate. Alignment moves only the imagery.</p>
</section>
<section class="card">
<p class="label">Layers</p>
<sc-for list="{{{{layers}}}}" as="l" hint-placeholder-count="8">
<button class="tog" type="button" aria-pressed="{{{{l.on}}}}"><span class="box" style="background: {{{{l.fill}}}};border-color: {{{{l.stroke}}}}"></span><span style="flex:1">{{{{l.name}}}}</span><span class="mono muted">{{{{l.n}}}}</span></button>
</sc-for>
</section>
<section class="card">
<p class="label">Selected</p>
<label for="fname" class="muted" style="font-size:12.5px">Name</label>
<input id="fname" class="field" type="text" defaultValue="No-go Zone 3">
<p class="muted" style="margin:10px 0 0;font-size:12.5px">All three no-go zones on the robot share this name. Renaming makes the history readable.</p>
<dl style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:12px 0 0">
<div><dt class="muted" style="font-size:12px">ID</dt><dd class="mono" style="margin:0">45</dd></div>
<div><dt class="muted" style="font-size:12px">Points</dt><dd class="mono" style="margin:0">16 → 17</dd></div>
<div><dt class="muted" style="font-size:12px">Enabled</dt><dd style="margin:0">Yes</dd></div>
</dl>
</section>
<section class="card">
<p class="label">Write to robot</p>
<ul style="list-style:none;margin:0 0 12px;padding:0">
<sc-for list="{{{{gates}}}}" as="g" hint-placeholder-count="5">
<li class="li"><span class="dot" style="background: {{{{g.color}}}}"></span><span style="flex:1">{{{{g.name}}}}</span><span class="mono muted" style="font-size:12.5px">{{{{g.v}}}}</span></li>
</sc-for>
</ul>
<button class="btn pri" type="button" disabled="{{{{yes}}}}" style="width:100%;background: {{{{accent}}}}">Write to robot</button>
<p class="muted" style="margin:8px 0 0;font-size:12.5px">Phase 4. Version history keeps v1 for one-click restore.</p>
</section>
</aside>
</div>""",
     """    return {
      accent, yes: true,
      layers: [
        { name: 'Imagery', n: 'Esri', on: 'true', fill: '#2C4527', stroke: '#5F6E67' },
        { name: 'Areas', n: '3', on: 'true', fill: '#24472F', stroke: '#8BD49A' },
        { name: 'No-go zones', n: '3', on: 'true', fill: '#33260F', stroke: '#F0B955' },
        { name: 'No-vision zones', n: '1', on: 'true', fill: 'transparent', stroke: '#79B8F0' },
        { name: 'Pathways', n: '6', on: 'true', fill: 'transparent', stroke: '#E6ECE8' },
        { name: 'Charging points', n: '2', on: 'true', fill: '#0E1311', stroke: '#8BD49A' },
        { name: 'Camera coverage', n: '3 of 6', on: 'true', fill: 'rgba(121,184,240,.16)', stroke: '#79B8F0' },
        { name: 'Trouble spots (Runs)', n: '—', on: 'false', fill: 'transparent', stroke: '#F08A6E' }
      ],
      gates: [
        { name: 'Robot docked and idle', v: 'yes', color: '#8BD49A' },
        { name: 'Snapshot of v1 saved', v: 'yes', color: '#8BD49A' },
        { name: 'Validation', v: '0 issues', color: '#8BD49A' },
        { name: 'Diff reviewed', v: 'not yet', color: '#F0B955' },
        { name: 'Write protocol known', v: 'C2 pending', color: '#F08A6E' }
      ]
    };""", 1520)

# ---------------------------------------------------------------- Schedule
SCH_CSS = r"""
.sw{position:relative;width:38px;height:22px;flex:0 0 38px;border-radius:999px;border:0;cursor:pointer;padding:0}
.knob{position:absolute;top:3px;width:16px;height:16px;border-radius:50%;background:#0E1311}
"""
page("Schedule", "Schedule", SCH_CSS,
     '<h1 class="h1">Schedule</h1><div class="seg" role="group" aria-label="Season profile"><button class="on" type="button" aria-pressed="true">Mower</button>'
     '<button type="button" aria-pressed="false">Snow</button><button type="button" aria-pressed="false">Leaf</button></div>' + chip("Lawn Mower Pro attached", "#8BD49A"),
     """<div style="display:flex;flex-wrap:wrap;gap:16px;padding:20px 24px;align-items:flex-start">
<section class="card" style="flex:1 1 560px;min-width:0" aria-label="Weekly schedule">
<div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:14px"><p class="label" style="margin:0 auto 0 0">Week</p><span class="ex">Example schedule</span><button class="btn" type="button">Add window</button></div>
<div style="display:flex;gap:6px">
<div style="flex:0 0 46px;padding-top:28px">
<sc-for list="{{hours}}" as="h" hint-placeholder-count="16"><div class="mono muted" style="height:28px;font-size:11px;line-height:1">{{h.label}}</div></sc-for>
</div>
<sc-for list="{{days}}" as="d" hint-placeholder-count="7">
<div style="flex:1 1 0;min-width:0">
<div style="height:28px;text-align:center;font-size:12.5px;font-weight:600;color:#C9D4CE">{{d.name}}</div>
<div style="position:relative;height:448px;border-radius:8px;background:#0F1512;overflow:hidden">
<sc-for list="{{hours}}" as="h" hint-placeholder-count="16"><div style="height:28px;border-top:1px solid #1B2420;box-sizing:border-box"></div></sc-for>
<sc-for list="{{d.blocks}}" as="b" hint-placeholder-count="1">
<div style="position:absolute;left:4px;right:4px;top: {{b.top}};height: {{b.height}};background: {{b.bg}};border:1px solid {{b.bd}};border-radius:7px;padding:5px 7px;box-sizing:border-box;font-size:12px;overflow:hidden"><div style="font-weight:600">{{b.title}}</div><div class="mono" style="font-size:11px;color:#C9D4CE">{{b.time}}</div></div>
</sc-for>
</div>
</div>
</sc-for>
</div>
<p class="muted" style="margin:12px 0 0;font-size:12.5px">A window is the time a plan may run. Inside it, the run starts as soon as every gate passes, or is skipped with a reason.</p>
</section>
<aside style="flex:0 1 400px;min-width:300px;display:flex;flex-direction:column;gap:16px" aria-label="Lawn conditions, gates and upcoming runs">
<section class="card">
<div style="display:flex;align-items:center;gap:8px"><p class="label" style="margin:0 auto 0 0">Lawn-dry gate</p><span class="chip">Tempest + robot + forecast</span></div>
<div style="display:flex;align-items:baseline;gap:12px;margin:8px 0 4px"><span class="big" style="color:#79B8F0">Wet</span><span class="muted">start when Dry, until the window ends</span></div>
<div class="kv"><span class="muted">Rain lookback</span><span class="mono">6 h, weighted by amount</span></div>
<div class="kv"><span class="muted">Dew risk</span><span class="mono">temp − dew point &lt; 2 °C</span></div>
<div class="kv"><span class="muted">Drying credit</span><span class="mono">sun · wind · temperature</span></div>
<div class="kv"><span class="muted">Earliest start on dewy days</span><span class="mono">sunrise + [learned]</span></div>
<div class="kv"><span class="muted">Robot rain sensor</span><span class="mono">veto while wet</span></div>
</section>
<section class="card">
<p class="label">Other gates</p>
<sc-for list="{{gates}}" as="g" hint-placeholder-count="7">
<div class="li"><span style="flex:1"><span style="display:block">{{g.name}}</span><span class="muted" style="font-size:12.5px">{{g.rule}}</span></span><button class="sw" type="button" role="switch" aria-checked="{{g.on}}" aria-label="{{g.name}}" style="background: {{g.track}}"><span class="knob" style="left: {{g.knob}}"></span></button></div>
</sc-for>
</section>
<section class="card">
<p class="label">When a run is due</p>
<div class="seg" role="group" aria-label="Start behaviour"><button class="on" type="button" aria-pressed="true">Notify to confirm</button><button type="button" aria-pressed="false">Start automatically</button></div>
<p class="muted" style="margin:10px 0 0;font-size:12.5px">The HA notification carries a live Frigate snapshot of the area about to be mowed.</p>
</section>
<section class="card">
<p class="label">Next up</p>
<sc-for list="{{upcoming}}" as="u" hint-placeholder-count="3">
<div class="li"><span class="dot" style="background: {{u.color}}"></span><span style="flex:1"><span style="display:block;font-weight:600">{{u.plan}}</span><span class="muted" style="font-size:12.5px">{{u.why}}</span></span><span class="mono muted" style="font-size:12.5px">{{u.when}}</span></div>
</sc-for>
</section>
</aside>
</div>""",
     """    const fmt = (h) => { const hh = Math.floor(h); const mm = Math.round((h - hh) * 60); return (hh < 10 ? '0' : '') + hh + ':' + (mm < 10 ? '0' : '') + mm; };
    const block = (start, end, title, lawn) => ({ top: ((start - 6) * 28 + 2) + 'px', height: ((end - start) * 28 - 4) + 'px', title: title, time: fmt(start) + '–' + fmt(end), bg: lawn ? '#24472F' : '#1D3446', bd: lawn ? '#8BD49A' : '#79B8F0' });
    const hours = [];
    for (let h = 6; h <= 21; h++) hours.push({ label: fmt(h) });
    const gate = (name, rule, on) => ({ name: name, rule: rule, on: on ? 'true' : 'false', track: on ? accent : '#2E3A34', knob: on ? '19px' : '3px' });
    return {
      accent, hours,
      days: [
        { name: 'Mon', blocks: [] },
        { name: 'Tue', blocks: [block(9, 14, 'Mow All', true)] },
        { name: 'Wed', blocks: [] },
        { name: 'Thu', blocks: [] },
        { name: 'Fri', blocks: [block(9, 14, 'Mow All', true)] },
        { name: 'Sat', blocks: [block(9, 11, 'Mow Side Yard', false)] },
        { name: 'Sun', blocks: [] }
      ],
      gates: [
        gate('Rain forecast', 'below 40% over the next 6 h', true),
        gate('Yard clear (Frigate)', 'no person or pet in the area for 10 min', true),
        gate('Someone home', 'Alarmo not armed away', true),
        gate('Vacation mode', 'skip while on', true),
        gate('Temperature and wind', '45–95 °F, wind below [x] mph', true),
        gate('Quiet hours', 'none: night runs allowed', false),
        gate('Abort if everyone leaves', 'return to dock', true)
      ],
      upcoming: [
        { plan: 'Mow All', why: 'Waiting: lawn wet, est. dry [time]', when: 'Tue window', color: '#79B8F0' },
        { plan: 'Mow All', why: 'Gates pass on the current forecast', when: 'Fri window', color: '#8BD49A' },
        { plan: 'Mow Side Yard', why: 'Gates pass on the current forecast', when: 'Sat window', color: '#8BD49A' }
      ]
    };""", 1260)

# ---------------------------------------------------------------- Insights
INS_CSS = r"""
.cols{display:flex;align-items:flex-end;gap:8px;height:150px;padding-top:10px;border-bottom:1px solid #2A3530}
.col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;gap:2px;height:100%}
.hbar{height:14px;border-radius:3px}
"""
page("Insights", "Insights", INS_CSS,
     '<h1 class="h1">Insights</h1><div class="seg" role="group" aria-label="Period"><button class="on" type="button" aria-pressed="true">Week</button>'
     '<button type="button" aria-pressed="false">Month</button><button type="button" aria-pressed="false">Season</button><button type="button" aria-pressed="false">Year</button></div>'
     '<span class="ex">Example data throughout</span>',
     """<div style="display:flex;flex-wrap:wrap;gap:16px;padding:20px 24px;align-items:flex-start">
<div style="flex:1 1 620px;min-width:0;display:flex;flex-direction:column;gap:16px">
<div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px">
<sc-for list="{{kpis}}" as="k" hint-placeholder-count="4">
<div class="card"><p class="label" style="margin-bottom:4px">{{k.label}}</p><div class="mono" style="font-size:24px">{{k.v}}</div><div class="muted" style="font-size:12px">{{k.sub}}</div></div>
</sc-for>
</div>
<section class="card" aria-label="Mowing hours per week">
<div style="display:flex;align-items:center;gap:8px"><p class="label" style="margin:0 auto 0 0">Mowing hours per week</p><span class="muted" style="font-size:12.5px">stacked by outcome</span></div>
<div class="cols">
<sc-for list="{{weeks}}" as="w" hint-placeholder-count="12">
<div class="col" title="{{w.label}}"><div style="height: {{w.skip}};background:#1D3446;border-radius:3px 3px 0 0"></div><div style="height: {{w.done}};background: {{accent}};border-radius:0 0 2px 2px"></div></div>
</sc-for>
</div>
<div style="display:flex;gap:8px;margin-top:6px"><sc-for list="{{weeks}}" as="w" hint-placeholder-count="12"><div class="mono muted" style="flex:1;text-align:center;font-size:11px">{{w.label}}</div></sc-for></div>
<div style="display:flex;gap:16px;margin-top:10px;font-size:12.5px;color:#C9D4CE"><span style="display:inline-flex;align-items:center;gap:8px"><span style="width:12px;height:12px;border-radius:3px;background: {{accent}}"></span>Mowed</span><span style="display:inline-flex;align-items:center;gap:8px"><span style="width:12px;height:12px;border-radius:3px;background:#1D3446"></span>Skipped window</span></div>
</section>
<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px">
<section class="card" aria-label="Time by zone">
<p class="label">Time by zone</p>
<sc-for list="{{zones}}" as="z" hint-placeholder-count="3">
<div style="margin:10px 0"><div style="display:flex;justify-content:space-between;font-size:13px"><span>{{z.name}}</span><span class="mono muted">{{z.v}}</span></div><div class="bar" style="margin-top:6px"><div class="fill" style="width: {{z.w}};background: {{accent}}"></div></div></div>
</sc-for>
</section>
<section class="card" aria-label="Why windows were skipped">
<p class="label">Why windows were skipped</p>
<sc-for list="{{skips}}" as="s" hint-placeholder-count="4">
<div style="margin:10px 0"><div style="display:flex;justify-content:space-between;font-size:13px"><span>{{s.name}}</span><span class="mono muted">{{s.v}}</span></div><div class="bar" style="margin-top:6px"><div class="fill" style="width: {{s.w}};background:#79B8F0"></div></div></div>
</sc-for>
</section>
</div>
<section class="card">
<p class="label">Positioning quality trend</p>
<div style="display:flex;align-items:flex-end;gap:4px;height:70px">
<sc-for list="{{rtk}}" as="r" hint-placeholder-count="12"><div style="flex:1;height: {{r.h}};background:#2F5A3B;border-top:2px solid #8BD49A"></div></sc-for>
</div>
<p class="muted" style="margin:8px 0 0;font-size:12.5px">Share of mowing time with an RTK fix, per week. A dip flags base-station or link trouble before runs start failing.</p>
</section>
</div>
<aside style="flex:0 1 380px;min-width:300px;display:flex;flex-direction:column;gap:16px" aria-label="Weekly digest">
<section class="card">
<p class="label">Weekly digest preview</p>
<div style="background:#0F1512;border:1px solid #24302A;border-radius:12px;padding:14px">
<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px"><span style="width:26px;height:26px;border-radius:7px;background:#18231E;display:inline-flex;align-items:center;justify-content:center;color:#8BD49A"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true">""" + ICONS["chart"] + """</svg></span><b>Yarbo · week of [date]</b></div>
<p style="margin:0 0 8px">[3] runs: [2] completed, [1] skipped (lawn wet). [x] m² mowed in [h] h.</p>
<p style="margin:0 0 8px">RTK fixed [x]% of the time. A new trouble spot is forming by the tree ring.</p>
<p style="margin:0 0 8px">[1] gap touched up. Blades: about [x] h until inspection. Battery health 100%.</p>
<p class="muted" style="margin:0">Next window: [day] — forecast looks dry.</p>
</div>
<p class="label" style="margin-top:16px">Send to</p>
<div style="display:flex;flex-wrap:wrap;gap:8px"><span class="chip"><span class="dot" style="background:#8BD49A"></span>HA mobile app</span><span class="chip">Telegram</span><span class="chip">Email</span><span class="chip"><span class="dot" style="background:#8BD49A"></span>Ask Claude (MCP)</span></div>
<label for="when" class="muted" style="font-size:12.5px;display:block;margin-top:14px">Schedule</label>
<select id="when" class="field"><option>Mondays, 7:00</option><option>Sundays, 18:00</option><option>Monthly, 1st</option></select>
</section>
<section class="card">
<p class="label">Season so far</p>
<div class="kv" style="border-top:0"><span class="muted">Runs</span><span class="mono">[—]</span></div>
<div class="kv"><span class="muted">Area mowed</span><span class="mono">[—] m²</span></div>
<div class="kv"><span class="muted">Hours</span><span class="mono">[—]</span></div>
<div class="kv"><span class="muted">vs last season</span><span class="mono">[—]</span></div>
<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px"><button class="btn" type="button">Export CSV</button><button class="btn" type="button">Season report</button></div>
</section>
</aside>
</div>""",
     """    const wk = [3.1, 2.4, 3.6, 0.8, 2.9, 3.3, 2.2, 3.8, 1.2, 3.0, 2.7, 3.4];
    const sk = [0.0, 1.0, 0.0, 2.2, 0.4, 0.0, 1.2, 0.0, 2.0, 0.3, 0.6, 0.0];
    const px = (v) => Math.round(v / 4.2 * 140) + 'px';
    const pct = (v, max) => Math.round(v / max * 100) + '%';
    return {
      accent,
      kpis: [
        { label: 'Runs', v: '3', sub: '2 completed · 1 skipped' },
        { label: 'Area', v: '[x] m²', sub: 'All Yard + side yard' },
        { label: 'Hours', v: '[h]', sub: 'robot working time' },
        { label: 'RTK fixed', v: '[x]%', sub: 'of mowing time' }
      ],
      weeks: wk.map((v, i) => ({ label: 'W' + (i + 1), done: px(v), skip: px(sk[i]) })),
      zones: [
        { name: 'All Yard', v: '[h]', w: pct(8.2, 8.2) },
        { name: 'side yard', v: '[h]', w: pct(1.1, 8.2) },
        { name: 'upper driveway', v: '[h]', w: pct(0.4, 8.2) }
      ],
      skips: [
        { name: 'Lawn wet', v: '[n]', w: '100%' },
        { name: 'Rain forecast', v: '[n]', w: '60%' },
        { name: 'Nobody home', v: '[n]', w: '35%' },
        { name: 'Person in yard (Frigate)', v: '[n]', w: '15%' }
      ],
      rtk: [92, 94, 91, 88, 93, 95, 90, 72, 85, 93, 94, 95].map((v) => ({ h: Math.round((v - 60) / 40 * 70) + 'px' }))
    };""", 1000)

# ---------------------------------------------------------------- Maintenance
page("Maintenance", "Maintenance", "",
     '<h1 class="h1">Maintenance</h1>' + chip("1 due soon", "#F0B955") + '<span class="ex">Example counters</span><button class="btn" type="button">Add item</button>',
     """<div style="display:flex;flex-wrap:wrap;gap:16px;padding:20px 24px;align-items:flex-start">
<div style="flex:1 1 620px;min-width:0;display:flex;flex-direction:column;gap:12px">
<sc-for list="{{items}}" as="it" hint-placeholder-count="6">
<section class="card" style="display:flex;flex-wrap:wrap;align-items:center;gap:16px">
<div style="flex:1 1 260px;min-width:0">
<div style="display:flex;align-items:center;gap:8px"><span class="dot" style="background: {{it.color}}"></span><b>{{it.name}}</b><span class="tag" style="background:#1E2924;color:#C9D4CE">{{it.kind}}</span></div>
<div class="muted" style="font-size:12.5px;margin-top:2px">{{it.rule}}</div>
<div class="bar" style="margin-top:10px"><div class="fill" style="width: {{it.pct}};background: {{it.color}}"></div></div>
<div class="mono muted" style="font-size:12px;margin-top:6px">{{it.used}}</div>
</div>
<button class="btn" type="button">Mark done</button>
</section>
</sc-for>
</div>
<aside style="flex:0 1 380px;min-width:300px;display:flex;flex-direction:column;gap:16px" aria-label="Blade dullness and service log">
<section class="card">
<p class="label">Blade dullness index</p>
<div style="display:flex;align-items:baseline;gap:10px"><span class="big">[1.0]</span><span class="muted">vs baseline after the last blade change</span></div>
<div style="display:flex;align-items:flex-end;gap:4px;height:70px;margin-top:12px">
<sc-for list="{{dull}}" as="d" hint-placeholder-count="12"><div style="flex:1;height: {{d.h}};background: {{d.c}};border-radius:2px 2px 0 0"></div></sc-for>
</div>
<p class="muted" style="margin:10px 0 0;font-size:12.5px">Blade-motor current at matched rpm and grass load. A rising index means dull blades, so the reminder can fire before the hours timer.</p>
</section>
<section class="card">
<p class="label">Service log</p>
<div class="li" style="border-top:0"><span class="mono muted" style="width:86px">[date]</span><span style="flex:1">Blades replaced</span></div>
<div class="li"><span class="mono muted" style="width:86px">[date]</span><span style="flex:1">Deck cleaned</span></div>
<div class="li"><span class="mono muted" style="width:86px">[date]</span><span style="flex:1">Snow blower stored, mower head on</span></div>
<p class="muted" style="margin:10px 0 0;font-size:12.5px">Intervals start as placeholders; fill them from Yarbo's manual.</p>
</section>
</aside>
</div>""",
     """    const it = (name, kind, rule, used, p, color) => ({ name, kind, rule, used, pct: p + '%', color });
    return {
      accent,
      items: [
        it('Blades: inspect or replace', 'blade hours', 'every [x] h of blade-motor time, or sooner if the dullness index rises', '[used] of [x] h', 78, '#F0B955'),
        it('Deck cleaning', 'mows', 'every [x] mows, more often in wet weeks', '[used] of [x] mows', 45, '#8BD49A'),
        it('Wheels and treads', 'distance', 'every [x] km of wheel travel', '[used] of [x] km', 30, '#8BD49A'),
        it('Camera and lidar lenses', 'days', 'every [x] days', '[used] of [x] days', 55, '#8BD49A'),
        it('Battery health review', 'charge cycles', 'every [x] cycles: compare health and cell temperature spread', '[used] of [x] cycles', 20, '#8BD49A'),
        it('Season service: mower to snow', 'head change', 'checklist runs when the snow blower is attached', 'next: when the head changes', 0, '#79B8F0')
      ],
      dull: [1.0, 1.0, 1.01, 1.02, 1.02, 1.03, 1.05, 1.06, 1.08, 1.1, 1.13, 1.16].map((v) => ({ h: Math.round((v - 0.9) / 0.3 * 70) + 'px', c: v > 1.1 ? '#F0B955' : '#2F5A3B' }))
    };""", 920)

# ---------------------------------------------------------------- Health
page("Health", "Health", "",
     '<h1 class="h1">Health</h1>' + chip("Snapshot 2026-09-30 21:31") + chip("No active errors", "#8BD49A") + chip("1 to watch", "#F0B955"),
     """<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;padding:20px 24px;align-items:start">
<section class="card"><p class="label">Positioning</p><div class="big" style="color:#F0B955">No RTK fix</div>
<div class="muted" style="font-size:12.5px;margin-bottom:8px">Single-point (GGA 1) while docked. Check whether that's normal at idle.</div>
<div class="kv"><span class="muted">Satellites</span><span class="mono">26</span></div><div class="kv"><span class="muted">Heading DOP</span><span class="mono">1.18</span></div>
<div class="kv"><span class="muted">RTCM age</span><span class="mono">none received</span></div><div class="kv"><span class="muted">Base station</span><span class="mono">OK</span></div>
<div class="kv"><span class="muted">Advanced fusion</span><span class="mono">on</span></div></section>
<section class="card"><p class="label">Link</p><div class="big">-57 dBm</div><div class="muted" style="font-size:12.5px;margin-bottom:8px">HaLow to the Data Center, connected</div>
<div class="kv"><span class="muted">Route in use</span><span class="mono">HaLow</span></div><div class="kv"><span class="muted">Wi-Fi / 4G</span><span class="mono">standby</span></div>
<div class="kv"><span class="muted">Cloud session</span><span class="mono">valid · renews itself</span></div></section>
<section class="card"><p class="label">Battery</p><div style="display:flex;align-items:baseline;gap:10px"><span class="big">95%</span><span class="muted">health 100% · 41.8 V</span></div>
<div style="display:flex;align-items:flex-end;gap:8px;height:84px;margin:14px 0 6px" role="img" aria-label="Six battery cell temperatures, 24 to 25 degrees Celsius">
<sc-for list="{{cells}}" as="c" hint-placeholder-count="6"><div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px"><div style="width:100%;height: {{c.h}};border-radius:5px 5px 2px 2px;background:#2F5A3B;border:1px solid #8BD49A;box-sizing:border-box"></div><span class="mono muted" style="font-size:11.5px">{{c.t}}°</span></div></sc-for>
</div><div class="muted" style="font-size:12.5px">Cell temperatures, °C</div></section>
<section class="card"><p class="label">Drive and deck</p>
<div class="kv"><span class="muted">Wheel current L / R</span><span class="mono">0.06 / 0.03 A</span></div><div class="kv"><span class="muted">Left blade motor</span><span class="mono">59 °C</span></div>
<div class="kv"><span class="muted">Right blade motor</span><span class="mono">58 °C</span></div><div class="kv"><span class="muted">Deck lift position</span><span class="mono">50</span></div>
<div class="kv"><span class="muted">Odometer L / R</span><span class="mono">6,338 / 6,374</span></div></section>
<section class="card"><p class="label">Wheel slip (soggy spots)</p><div class="big">[—]</div>
<p class="muted" style="margin:6px 0 0;font-size:12.5px">Wheel distance vs RTK distance per grid cell. Persistent slip marks wet ground and future ruts, and feeds back into the lawn-dry model.</p></section>
<section class="card"><p class="label">Firmware</p>
<sc-for list="{{fw}}" as="f" hint-placeholder-count="6"><div class="kv"><span class="muted">{{f.k}}</span><span class="mono">{{f.v}}</span></div></sc-for>
<div class="muted" style="font-size:12px;margin-top:6px">A change alerts HA and triggers a protocol re-check.</div></section>
<section class="card" style="grid-column:span 2"><p class="label">Error dictionary</p>
<p class="muted" style="margin:0 0 6px;font-size:12.5px">Seeded from the HA integration's labels, filled in as errors are seen.</p>
<sc-for list="{{errs}}" as="e" hint-placeholder-count="5"><div class="kv" style="justify-content:flex-start"><span class="mono" style="width:64px;flex:0 0 64px;color:#F5B3A2">{{e.code}}</span><span style="flex:1">{{e.what}}</span><span class="muted" style="font-size:12.5px">{{e.fix}}</span></div></sc-for>
</section>
<section class="card"><p class="label">Compute</p>
<div class="kv"><span class="muted">CPU temperature</span><span class="mono">44 °C</span></div><div class="kv"><span class="muted">CPU load</span><span class="mono">27%</span></div>
<div class="kv"><span class="muted">Memory free</span><span class="mono">4.7 of 8 GB</span></div></section>
</div>""",
     """    const temps = [24, 24, 25, 25, 24, 25];
    return {
      accent,
      cells: temps.map((t) => ({ t: String(t), h: Math.round((t / 30) * 64) + 'px' })),
      fw: [ { k: 'Body', v: '3.2.35' }, { k: 'Head', v: '2.2.10' }, { k: 'Chassis', v: '2.1.46' }, { k: 'Data Center', v: '1.0.25' }, { k: 'BLE', v: '1.1.59.4' }, { k: 'Cameras', v: 'V1.0.5 to V1.1.3' } ],
      errs: [
        { code: 'WP003', what: 'Plan not found', fix: 'Refresh plans; re-select' },
        { code: 'WP006', what: 'Outside mapped area', fix: 'Drive back inside, or check the map' },
        { code: 'WP013', what: 'Location data exception', fix: 'Check RTK and the base station' },
        { code: 'WP017', what: 'Exceeded boundary', fix: 'Check the boundary near the last position' },
        { code: 'REC005', what: 'Recharge failed', fix: 'Check the dock approach path' }
      ]
    };""", 1000)

# ---------------------------------------------------------------- Lab
LAB_CSS = r"""
.sess{display:flex;flex-direction:column;gap:2px;width:100%;min-height:44px;text-align:left;padding:10px 12px;border-radius:9px;border:1px solid transparent;color:#E6ECE8;font:inherit;cursor:pointer}
"""
page("Lab", "Protocol Lab", LAB_CSS,
     '<h1 class="h1">Protocol Lab</h1>' + chip("Listening: local + cloud", "#8BD49A") + chip("Never publishes during capture")
     + '<button class="btn pri" type="button" style="background: {{accent}}">New capture session</button>',
     """<div style="display:flex;flex-wrap:wrap;gap:16px;padding:20px 24px;align-items:flex-start">
<section class="card" style="flex:0 1 280px;min-width:240px" aria-label="Capture sessions"><p class="label">Sessions</p>
<div style="display:flex;flex-direction:column;gap:4px">
<sc-for list="{{sessions}}" as="s" hint-placeholder-count="7">
<button class="sess" type="button" style="background: {{s.bg}};border-color: {{s.bd}}">
<span style="display:flex;justify-content:space-between;gap:8px"><span class="mono" style="font-weight:600">{{s.id}}</span><span class="tag" style="background: {{s.tagbg}};color: {{s.tagfg}}">{{s.status}}</span></span>
<span style="font-size:13px">{{s.what}}</span><span class="muted" style="font-size:12px">{{s.meta}}</span>
</button>
</sc-for>
</div></section>
<section class="card" style="flex:1 1 520px;min-width:0" aria-label="C1 message stream">
<div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:10px"><p class="label" style="margin:0 auto 0 0">C1 · Smart Vision · 2026-09-30</p><span class="chip">Hiding DeviceMSG, heart_beat, polling</span><span class="chip">416 messages</span></div>
<table style="width:100%;border-collapse:collapse;font-size:12.5px;table-layout:fixed">
<thead><tr><th scope="col" class="muted" style="text-align:left;font-weight:500;width:96px;padding:4px 0">Time</th><th scope="col" class="muted" style="text-align:left;font-weight:500;width:58px">Broker</th><th scope="col" class="muted" style="text-align:left;font-weight:500;width:190px">Topic</th><th scope="col" class="muted" style="text-align:left;font-weight:500">Payload</th></tr></thead>
<tbody>
<sc-for list="{{msgs}}" as="m" hint-placeholder-count="10">
<tr><td class="mono muted" style="padding:7px 0;border-top:1px solid #1E2723">{{m.t}}</td><td style="border-top:1px solid #1E2723">{{m.b}}</td><td class="mono" style="border-top:1px solid #1E2723;color: {{m.color}};overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{m.topic}}</td><td class="mono" style="border-top:1px solid #1E2723;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{m.p}}</td></tr>
</sc-for>
</tbody></table></section>
<aside style="flex:0 1 360px;min-width:300px;display:flex;flex-direction:column;gap:16px" aria-label="Commands, cameras and console">
<section class="card"><p class="label">Command explorer</p>
<sc-for list="{{cats}}" as="c" hint-placeholder-count="4"><div class="li" style="align-items:flex-start"><span class="tag" style="background: {{c.bg}};color: {{c.fg}};width:64px;text-align:center">{{c.name}}</span><span class="mono" style="flex:1;font-size:12.5px;line-height:1.6">{{c.cmds}}</span></div></sc-for>
</section>
<section class="card"><div style="display:flex;align-items:center;gap:8px;margin-bottom:10px"><p class="label" style="margin:0 auto 0 0">Robot cameras</p><span class="tag" style="background:#3A2E14;color:#F7D38E">Investigate further</span></div>
<ul style="margin:0;padding-left:18px;font-size:13px;display:flex;flex-direction:column;gap:6px">
<li>4 IP cameras on the robot's internal network</li>
<li>Live view toggle is <span class="mono">smart_vision_control</span>; no stream details on MQTT</li>
<li>Next: watch the phone's connections in UniFi during a live view</li>
<li>Then a gentle port check of the robot while it streams</li>
</ul></section>
<section class="card"><p class="label">Console</p>
<label for="cmd" class="muted" style="font-size:12.5px">Command</label><input id="cmd" class="field mono" type="text" defaultValue="read_low_cfd_config">
<label for="payload" class="muted" style="font-size:12.5px;display:block;margin-top:10px">Payload</label><input id="payload" class="field mono" type="text" defaultValue="{}">
<div style="display:flex;align-items:center;gap:10px;margin-top:12px"><button class="btn" type="button">Send read</button><span class="muted" style="font-size:12.5px">Reads run freely. Control needs typed confirmation. cmd_vel is blocked.</span></div>
</section>
</aside>
</div>""",
     """    const done = { tagbg: '#1F3A28', tagfg: '#B6E6C0' };
    const planned = { tagbg: '#1E2924', tagfg: '#C9D4CE' };
    const sel = { bg: '#18231E', bd: '#2E3A34' };
    const rest = { bg: 'transparent', bd: 'transparent' };
    const app = '#B6E6C0';
    const fb = '#A9D2F5';
    return {
      accent,
      sessions: [
        { id: 'C1', status: 'done', what: 'Smart Vision opened', meta: '2026-09-30 21:31 · both brokers', ...done, ...sel },
        { id: 'C2', status: 'planned', what: 'Add and delete a test no-go zone', meta: 'learns the map write', ...planned, ...rest },
        { id: 'C3', status: 'planned', what: 'Nudge and revert an area edge', meta: 'learns the area write', ...planned, ...rest },
        { id: 'C4', status: 'planned', what: 'Create, edit, delete a plan', meta: 'learns plan CRUD', ...planned, ...rest },
        { id: 'C5', status: 'planned', what: 'Create and delete a schedule', meta: 'learns schedules', ...planned, ...rest },
        { id: 'C6', status: 'planned', what: 'Start, stop, dock', meta: 'confirms SDK payloads', ...planned, ...rest },
        { id: 'C7', status: 'planned', what: 'Spot or waypoint mow', meta: 'unlocks gap touch-ups', ...planned, ...rest }
      ],
      msgs: [
        { t: '21:31:13.570', b: 'cloud', topic: 'app/set_working_state', p: '{"state":1}', color: app },
        { t: '21:31:43.047', b: 'cloud', topic: 'app/cmd_vel', p: '{"vel":0.0,"rev":0.0}  blocked', color: '#F5B3A2' },
        { t: '21:31:43.154', b: 'cloud', topic: 'app/smart_vision_control', p: '{"state":0}', color: app },
        { t: '21:31:43.839', b: 'cloud', topic: 'device/data_feedback', p: 'Smart vision control command sent: 0', color: fb },
        { t: '21:31:44.860', b: 'cloud', topic: 'app/read_low_cfd_config', p: '{}', color: app },
        { t: '21:31:45.503', b: 'cloud', topic: 'device/data_feedback', p: 'area_low_cfd_configs: 3 areas, fusion on', color: fb },
        { t: '21:31:53.133', b: 'cloud', topic: 'app/read_tow_params', p: '{}', color: app },
        { t: '21:31:53.405', b: 'cloud', topic: 'device/data_feedback', p: '{"enable":false,"move_speed":0.4}', color: fb },
        { t: '21:31:56.023', b: 'cloud', topic: 'app/smart_vision_control', p: '{"state":1}', color: app },
        { t: '21:31:56.491', b: 'cloud', topic: 'device/data_feedback', p: 'Smart vision control command sent: 1', color: fb }
      ],
      cats: [
        { name: 'read', bg: '#1F3A28', fg: '#B6E6C0', cmds: 'get_device_msg · get_map · read_all_plan · read_gps_ref · read_low_cfd_config · read_tow_params' },
        { name: 'control', bg: '#1D3446', fg: '#A9D2F5', cmds: 'start_plan · pause · resume · stop · cmd_recharge · light_ctrl · mower_target_cmd · smart_vision_control' },
        { name: 'write', bg: '#3A2E14', fg: '#F7D38E', cmds: 'map / plan / schedule / spot: unknown until C2-C5, C7' },
        { name: 'blocked', bg: '#3D211B', fg: '#F5B3A2', cmds: 'cmd_vel (manual drive)' }
      ]
    };""", 960)

# ---------------------------------------------------------------- Settings
SET_CSS = r"""
.step{display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;font-size:12.5px;font-weight:600;white-space:nowrap}
.cb{display:flex;align-items:center;gap:10px;min-height:44px;border-top:1px solid #1E2723;font-size:13px}
.cb input{width:18px;height:18px;accent-color:#8BD49A}
"""
page("Settings", "Settings", SET_CSS,
     '<h1 class="h1">Settings</h1>' + chip("Setup wizard: 4 of 6 done") + '<button class="btn" type="button">Export YAML</button><button class="btn" type="button">Import</button>',
     """<div style="padding:16px 24px 0;display:flex;flex-wrap:wrap;gap:8px" aria-label="Setup progress">
<sc-for list="{{steps}}" as="s" hint-placeholder-count="6"><span class="step" style="background: {{s.bg}};color: {{s.fg}}"><span class="dot" style="background: {{s.dot}}"></span>{{s.name}}</span></sc-for>
</div>
<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;padding:16px 24px 24px;align-items:start">
<section class="card"><div style="display:flex;align-items:center;gap:8px"><p class="label" style="margin:0 auto 0 0">Robot</p><span class="tag" style="background:#1F3A28;color:#B6E6C0">connected</span></div>
<div class="kv" style="margin-top:8px"><span class="muted">Cloud account</span><span class="mono">signed in · owner</span></div>
<div class="kv"><span class="muted">Session</span><span class="mono">renews itself</span></div>
</section>
<section class="card"><div style="display:flex;align-items:center;gap:8px"><p class="label" style="margin:0 auto 0 0">Home Assistant</p><span class="tag" style="background:#1F3A28;color:#B6E6C0">connected</span></div>
<label for="ha" class="muted" style="font-size:12.5px;display:block;margin-top:8px">URL</label><input id="ha" class="field mono" type="text" defaultValue="http://192.0.2.20:8123">
<div class="kv" style="margin-top:10px"><span class="muted">Token</span><span class="mono">from .env</span></div>
<div class="kv"><span class="muted">Presence</span><span class="mono">alarm_control_panel.home</span></div>
<div class="kv"><span class="muted">Forecast</span><span class="mono">weather.home</span></div></section>
<section class="card"><div style="display:flex;align-items:center;gap:8px"><p class="label" style="margin:0 auto 0 0">Weather sources</p><span class="tag" style="background:#1F3A28;color:#B6E6C0">auto-matched</span></div>
<sc-for list="{{wx}}" as="w" hint-placeholder-count="6"><div class="kv"><span class="muted">{{w.role}}</span><span class="mono">{{w.src}}</span></div></sc-for>
<p class="muted" style="margin:10px 0 0;font-size:12.5px">Without HA, the app can read a Tempest hub's local broadcast (UDP 50222) directly.</p></section>
<section class="card" style="grid-column:span 2"><div style="display:flex;align-items:center;gap:8px"><p class="label" style="margin:0 auto 0 0">Frigate (follow-cam and yard safety)</p><span class="tag" style="background:#3A2E14;color:#F7D38E">2 steps left</span></div>
<div style="display:flex;flex-wrap:wrap;gap:16px;margin-top:8px">
<div style="flex:1 1 260px;min-width:0">
<label for="fr" class="muted" style="font-size:12.5px">URL</label><input id="fr" class="field mono" type="text" defaultValue="http://192.0.2.10:5555">
<div class="kv" style="margin-top:10px"><span class="muted">Cameras found</span><span class="mono">12</span></div>
<div class="kv"><span class="muted">Frigate+</span><span class="mono">enabled</span></div>
<div class="kv"><span class="muted">robot_lawnmower tracked on</span><span class="mono" style="color:#F7D38E">none yet</span></div>
<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px"><button class="btn" type="button">Track robot_lawnmower on selected</button><button class="btn" type="button">Run calibration mow</button></div>
</div>
<fieldset style="flex:1 1 240px;min-width:0;border:0;margin:0;padding:0"><legend class="label">Yard cameras</legend>
<sc-for list="{{cams}}" as="c" hint-placeholder-count="6"><label class="cb"><input type="checkbox" defaultChecked="{{c.on}}"><span class="mono" style="flex:1">{{c.name}}</span><span class="muted" style="font-size:12px">{{c.note}}</span></label></sc-for>
</fieldset>
</div></section>
<section class="card"><div style="display:flex;align-items:center;gap:8px"><p class="label" style="margin:0 auto 0 0">Imagery</p><span class="tag" style="background:#3A2E14;color:#F7D38E">not aligned</span></div>
<div class="kv" style="margin-top:8px"><span class="muted">Provider</span><span class="mono">Esri World Imagery</span></div>
<div class="kv"><span class="muted">Fetched by</span><span class="mono">server (cached)</span></div>
<a class="btn" href="MapStudio.dc.html" style="margin-top:12px">Align in Map Studio</a></section>
<section class="card"><div style="display:flex;align-items:center;gap:8px"><p class="label" style="margin:0 auto 0 0">Notifications</p><span class="tag" style="background:#1E2924;color:#C9D4CE">optional</span></div>
<label class="cb"><input type="checkbox" defaultChecked="{{yes}}"><span style="flex:1">HA mobile app</span></label>
<label class="cb"><input type="checkbox"><span style="flex:1">Telegram</span></label>
<label class="cb"><input type="checkbox"><span style="flex:1">Email</span></label>
<label class="cb"><input type="checkbox" defaultChecked="{{yes}}"><span style="flex:1">Weekly digest</span></label></section>
<section class="card"><div style="display:flex;align-items:center;gap:8px"><p class="label" style="margin:0 auto 0 0">Data</p></div>
<div class="kv" style="margin-top:8px"><span class="muted">Telemetry</span><span class="mono">kept</span></div>
<div class="kv"><span class="muted">Raw snapshots</span><span class="mono">90 days</span></div>
<div class="kv"><span class="muted">Map versions</span><span class="mono">all</span></div>
<div class="kv"><span class="muted">Leaves this server</span><span class="mono">never</span></div></section>
<section class="card"><div style="display:flex;align-items:center;gap:8px"><p class="label" style="margin:0 auto 0 0">Integrations out</p></div>
<label class="cb"><input type="checkbox" defaultChecked="{{yes}}"><span style="flex:1">HA entities and alerts</span></label>
<label class="cb"><input type="checkbox" defaultChecked="{{yes}}"><span style="flex:1">MCP server (reads; control behind interlocks)</span></label>
<label class="cb"><input type="checkbox"><span style="flex:1">Webhooks</span></label></section>
</div>""",
     """    const st = (name, state) => state === 'done'
      ? { name, bg: '#18231E', fg: '#C9D4CE', dot: '#8BD49A' }
      : state === 'now'
        ? { name, bg: '#3A2E14', fg: '#F7D38E', dot: '#F0B955' }
        : { name, bg: '#141B18', fg: '#A3B1AA', dot: '#5F6E67' };
    return {
      accent, yes: true,
      steps: [st('Robot', 'done'), st('Home Assistant', 'done'), st('Weather', 'done'), st('Frigate', 'now'), st('Imagery', 'next'), st('Notifications', 'done')],
      wx: [
        { role: 'Rain today / last hour', src: 'Tempest (via HA)' },
        { role: 'Raining now', src: 'Tempest (via HA)' },
        { role: 'Dew point, humidity', src: 'Tempest (via HA)' },
        { role: 'Solar radiation', src: 'Tempest (via HA)' },
        { role: 'Backup station', src: 'nearby PWS (via HA)' },
        { role: 'Rain on the robot', src: 'robot rain sensor' }
      ],
      cams: [
        { name: 'back_yard', on: true, note: 'yard' },
        { name: 'back_deck', on: true, note: 'yard' },
        { name: 'garage', on: true, note: 'driveway' },
        { name: 'front', on: true, note: 'front lawn' },
        { name: 'doorbell', on: false, note: 'porch' },
        { name: 'service_door', on: false, note: 'side' }
      ]
    };""", 1100)

print("wrote", sorted(p.name for p in OUT.glob("*.dc.html")))
