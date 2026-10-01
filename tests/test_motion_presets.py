"""Animation > Previews: the motion presets (pages/text_animator/motion.py) and
putting them on clips (motion_resolve.py) against a fake Fusion comp that wires
the way Studio 21.1 did when driven live: a Merge slotted in front of MediaOut
over a timeline-sized canvas, each channel a BezierSpline written whole with
SetKeyFrames, Center through an XYPath, and every change followed by the
ExportFusionComp + ImportFusionComp round trip the Edit page needs."""

import copy
import re
import unittest
from unittest import mock

import _paths  # noqa: F401
from pages.text_animator import framing, motion, motion_resolve as mr

PRESETS = {p["id"]: p for p in motion.load()}


# ------------------------------------------------------------ fake Fusion --

class Output:
    def __init__(self, tool, name="Output"):
        self.tool, self.name = tool, name

    def GetTool(self):
        return self.tool


class Input:
    def __init__(self, tool, name):
        self.tool, self.name, self.source, self.value, self.expression = tool, name, None, None, None

    def SetExpression(self, text):
        self.expression = text

    def GetExpression(self):
        return self.expression

    def ConnectTo(self, output):
        self.source = output
        return True

    def GetConnectedOutput(self):
        return self.source


class Tool:
    def __init__(self, comp, reg_id, name):
        self.__dict__.update(comp=comp, reg_id=reg_id, name=name, inputs={}, data={}, keyframes=None,
                             Output=Output(self), Value=Output(self, "Value"))

    def __getattr__(self, name):
        if name[0].isupper():
            return self.inputs.setdefault(name, Input(self, name))
        raise AttributeError(name)

    def GetAttrs(self, key=None):
        attrs = {"TOOLS_RegID": self.reg_id, "TOOLS_Name": self.name}
        return attrs[key] if key else attrs

    def SetAttrs(self, attrs):
        if "TOOLS_Name" in attrs:
            self.comp.rename(self, attrs["TOOLS_Name"])

    def SetInput(self, name, value, *_):
        getattr(self, name).value = value

    def GetInput(self, name, *_):
        return getattr(self, name).value

    def ConnectInput(self, name, tool):
        getattr(self, name).ConnectTo(tool.Output)
        return True

    def GetData(self, key):
        return self.data.get(key)

    def SetData(self, key, value):
        self.data[key] = value

    def SetKeyFrames(self, table, replace):
        self.__dict__["keyframes"] = dict(table)

    def AddModifier(self, name, reg_id):
        mod = self.comp.AddTool(reg_id)
        getattr(self, name).ConnectTo(mod.Output)
        return True

    def Delete(self):
        self.comp.tools.pop(self.name, None)
        for tool in self.comp.tools.values():
            for inp in tool.inputs.values():
                if inp.source is not None and inp.source.tool is self:
                    inp.source = None


class Comp:
    def __init__(self, upstream="MediaIn", frames=None, size=(1920, 1080)):
        self.tools, self.counts, self.locked, self.frames, self.size = {}, {}, 0, frames, size
        head = self.AddTool(upstream)
        self.AddTool("MediaOut").ConnectInput("Input", head)

    def AddTool(self, reg_id, *_):
        n = self.counts[reg_id] = self.counts.get(reg_id, 0) + 1
        tool = Tool(self, reg_id, f"{reg_id}{n}")
        self.tools[tool.name] = tool
        return tool

    def rename(self, tool, name):
        self.tools.pop(tool.name)
        tool.__dict__["name"] = name
        self.tools[name] = tool

    def FindTool(self, name):
        return self.tools.get(name)

    def GetToolList(self, _selected, reg_id=None):
        found = [t for t in self.tools.values() if reg_id in (None, t.reg_id)]
        return dict(enumerate(found, 1))

    def GetAttrs(self):
        attrs = {"COMPN_RenderStart": 0.0}
        if self.frames:
            attrs["COMPN_RenderEnd"] = float(self.frames - 1)
        return attrs

    def GetPrefs(self, key):
        return {"Comp.FrameFormat.Width": self.size[0], "Comp.FrameFormat.Height": self.size[1]}[key]

    def Lock(self): self.locked += 1
    def Unlock(self): self.locked -= 1
    def StartUndo(self, _name): pass
    def EndUndo(self, _keep): pass


class Item:
    """A timeline clip. Without a comp, AddFusionComp makes one at `size` -
    the source's size, the way Resolve does (a 641x479 still, a 4K clip)."""

    def __init__(self, comp=None, name="clip", size=(1920, 1080), **inspector):
        self.comps = [comp] if comp else []
        self.names = [f"Composition {i + 1}" for i in range(len(self.comps))]
        self.name, self.size, self.reloads, self.deleted = name, size, 0, []
        self.exported = {}          # path -> a copy of the comp, as the file would hold it
        self.refuse_delete = False
        self.props = {"ZoomX": 1.0, "ZoomY": 1.0, "ZoomGang": True, "Pan": 0.0, "Tilt": 0.0,
                      "RotationAngle": 0.0, "AnchorPointX": 0.0, "AnchorPointY": 0.0, "FlipX": False,
                      "FlipY": False, "CropLeft": 0.0, "CropRight": 0.0, "CropTop": 0.0, "CropBottom": 0.0,
                      "CropSoftness": 0.0, "CropRetain": False, "Scaling": 0, **inspector}

    def GetProperty(self, key=None):
        return dict(self.props) if key is None else self.props.get(key)

    def SetProperty(self, key, value):
        self.props[key] = value
        return True

    def GetName(self): return self.name
    def GetFusionCompCount(self): return len(self.comps)
    def GetFusionCompByIndex(self, i): return self.comps[i - 1]
    def GetFusionCompNameList(self): return list(self.names)
    def LoadFusionCompByName(self, name): return self.comps[self.names.index(name)]

    def AddFusionComp(self):
        self.comps.append(Comp(size=self.size))
        self.names.append(self._fresh_name())
        return self.comps[-1]

    def _fresh_name(self):
        n = 1
        while f"Composition {n}" in self.names:
            n += 1
        return f"Composition {n}"

    def ExportFusionComp(self, path, index):
        with open(path, "w") as f:
            f.write("Composition {}")
        self.exported[path] = copy.deepcopy(self.comps[index - 1])
        return True

    def ImportFusionComp(self, path):
        # As Resolve does: a new comp, added after the others.
        self.reloads += 1
        self.comps.append(copy.deepcopy(self.exported[path]))
        self.names.append(self._fresh_name())
        return self.comps[-1]

    def DeleteFusionCompByName(self, name):
        if self.refuse_delete or len(self.comps) == 1 or name not in self.names:
            return False            # never the last one (Studio 21.1)
        i = self.names.index(name)
        self.deleted.append(name)
        del self.comps[i], self.names[i]
        return True

    def RenameFusionCompByName(self, old, new):
        if old not in self.names or new in self.names:
            return False
        self.names[self.names.index(old)] = new
        return True


def chain(comp):
    """Tool names from MediaOut back up the Input/Foreground chain."""
    names, tool = [], comp.FindTool("MediaOut1")
    while tool is not None:
        names.append(tool.name)
        inp = tool.inputs.get("Input") or tool.inputs.get("Foreground")
        tool = inp.source.tool if inp and inp.source else None
    return names


def carrier(inp):
    """The carrier an input plays its keys from through its expression, or None."""
    if not inp.expression:
        return None
    return inp.tool.comp.FindTool(inp.expression.split(":", 1)[0])


def curve(inp):
    """The keyframes on the spline feeding an input - straight, or through the
    carrier its expression reads - or None."""
    held = carrier(inp)
    if held is not None:
        inp = held.Angle
    return inp.source.tool.keyframes if inp.source else None


def values(keys):
    return {f: k["value"] for f, k in keys.items()}


# ------------------------------------------------------------------ tests --

class PresetData(unittest.TestCase):
    def test_every_preset_loads_whole(self):
        self.assertEqual(len(PRESETS), 24)
        for p in PRESETS.values():
            for c in "xyrso":
                self.assertEqual(len(p[c]), motion.SAMPLES, (p["id"], c))
            self.assertIn(p["kind"], motion.KINDS)
            self.assertIn(p["pack"], [k["id"] for k in motion.PACKS])

    def test_each_kind_has_its_moves(self):
        for p in PRESETS.values():
            found = motion.moves(p)
            want = {motion.KIND_IN_OUT: {"in", "out"}, motion.KIND_EMPHASIS: {"emphasis"},
                    motion.KIND_OUT: {"out"}}[p["kind"]]
            self.assertEqual(set(found), want, p["id"])

    def test_pop_overshoot_is_one_move(self):
        # Pop's scale passes through 1 on its way to settling.
        self.assertEqual(motion.moves(PRESETS["pop"])["in"], (0, 10))


class Fitting(unittest.TestCase):
    def test_every_move_stays_within_tolerance_of_its_samples(self):
        for p in PRESETS.values():
            for move, (a, b) in motion.moves(p).items():
                for name, (c, value) in motion._CHANNELS.items():
                    points = [(float(k), value(p[c][k])) for k in range(a, b + 1)]
                    keys = {t: {"value": v, "lh": lh, "rh": rh}
                            for t, v, lh, rh in motion.fit(points, motion.TOLERANCE[name])}
                    for t, v in points:
                        self.assertLessEqual(abs(motion.evaluate(keys, t) - v), motion.TOLERANCE[name] + 1e-6,
                                             (p["id"], move, name, t))

    def test_far_fewer_keys_than_samples(self):
        keys = samples = 0
        for p in PRESETS.values():
            plan = motion.plan(p, 120, 24)
            for name, channel in plan.items():
                if name == "moves":
                    continue
                keys += len(channel)
                samples += sum(b - a + 1 for a, b in (motion.moves(p)[m] for m, *_ in plan["moves"]))
        self.assertLess(keys, samples * 0.4)

    def test_an_ease_is_a_few_keys_with_handles(self):
        slide = motion.plan(PRESETS["slide"], 120, 24)["Center.X"]
        self.assertLessEqual(len(slide), 8)             # an In and an Out, a few keys each
        first, second = list(slide.values())[:2]
        self.assertIsNone(first["lh"])
        self.assertIsNotNone(first["rh"])
        self.assertIsNotNone(second["lh"])

    def test_a_turn_gets_a_key(self):
        # Pop overshoots to 1.22: a key sits on the peak.
        size = values(motion.plan(PRESETS["pop"], 120, 24, way="in")["Size"])
        self.assertAlmostEqual(max(size.values()), 1.22)

    def test_keys_are_smooth_except_a_sharp_hit(self):
        # The curve Drop + bounce had: a spike between samples and a kink.
        for p in PRESETS.values():
            for name, channel in motion.plan(p, 120, 24).items():
                if name == "moves":
                    continue
                frames = sorted(channel)
                for before, f, after in zip(frames, frames[1:], frames[2:]):
                    k = channel[f]
                    if not (k["lh"] and k["rh"]):
                        continue
                    left = (k["value"] - k["lh"][1]) / (f - k["lh"][0])
                    right = (k["rh"][1] - k["value"]) / (k["rh"][0] - f)
                    step_in = k["value"] - channel[before]["value"]
                    step_out = channel[after]["value"] - k["value"]
                    if step_in * step_out < 0 and abs(left - right) > 1e-6:
                        continue            # a sharp hit (a bounce on the floor) keeps its corner
                    # the same slope both sides (to the handles' rounding)
                    self.assertAlmostEqual(left, right, delta=max(1e-4, 0.01 * abs(left)), msg=(p["id"], name, f))


    def test_no_curve_bulges_past_its_samples(self):
        for p in PRESETS.values():
            for move, (a, b) in motion.moves(p).items():
                for name, (c, value) in motion._CHANNELS.items():
                    points = [(float(k), value(p[c][k])) for k in range(a, b + 1)]
                    keys = {t: {"value": v, "lh": lh, "rh": rh}
                            for t, v, lh, rh in motion.fit(points, motion.TOLERANCE[name])}
                    frames = sorted(keys)
                    for f0, f1 in zip(frames, frames[1:]):
                        span = [v for t, v in points if f0 <= t <= f1]
                        for i in range(1, 20):
                            v = motion.evaluate(keys, f0 + (f1 - f0) * i / 20)
                            self.assertGreaterEqual(v, min(span) - 2 * motion.TOLERANCE[name], (p["id"], move, name, f0))
                            self.assertLessEqual(v, max(span) + 2 * motion.TOLERANCE[name], (p["id"], move, name, f0))

    def test_a_peak_gets_level_handles(self):
        # Drop + bounce's hop off the floor tops out on a key with level handles.
        y = motion.plan(PRESETS["drop"], 120, 24, way="in")["Center.Y"]
        frames = sorted(y)
        top = max(frames[1:-1], key=lambda f: y[f]["value"] if f > frames[1] else -1)
        lh, rh = y[top]["lh"], y[top]["rh"]
        self.assertAlmostEqual(lh[1], y[top]["value"], places=4)
        self.assertAlmostEqual(rh[1], y[top]["value"], places=4)

    def test_stretching_a_move_keeps_its_shape(self):
        short = motion.plan(PRESETS["whip"], 120, 24, way="in")["Center.X"]
        long = motion.plan(PRESETS["whip"], 120, 24, way="in", speed=0.5)["Center.X"]
        self.assertEqual(len(short), len(long))
        for (f1, k1), (f2, k2) in zip(short.items(), long.items()):
            self.assertAlmostEqual(k1["value"], k2["value"])
            self.assertAlmostEqual(f2, f1 * 2, delta=0.01)


class Planning(unittest.TestCase):
    def test_in_from_the_first_frame_out_to_the_last(self):
        keys = motion.plan(PRESETS["pop"], 120, 24)
        size = values(keys["Size"])
        self.assertEqual(min(size), 0)
        self.assertEqual(max(size), 119)
        self.assertEqual(size[0], 0.0)          # starts invisible
        self.assertEqual(size[119], 0.0)        # and ends so
        self.assertEqual([m for m, *_ in keys["moves"]], ["in", "out"])

    def test_only_the_channels_that_move(self):
        self.assertEqual({k for k in motion.plan(PRESETS["pop"], 120, 24) if k != "moves"}, {"Size"})
        self.assertEqual({k for k in motion.plan(PRESETS["whip"], 120, 24) if k != "moves"}, {"Center.X"})

    def test_just_the_in_or_just_the_out(self):
        only_in = motion.plan(PRESETS["pop"], 120, 24, way="in")["Size"]
        self.assertLess(max(only_in), 30)
        only_out = motion.plan(PRESETS["pop"], 120, 24, way="out")["Size"]
        self.assertGreater(min(only_out), 90)

    def test_speed(self):
        normal = motion.plan(PRESETS["pop"], 120, 24, way="in")["moves"][0]
        double = motion.plan(PRESETS["pop"], 120, 24, way="in", speed=2)["moves"][0]
        self.assertAlmostEqual(double[2], normal[2] / 2, delta=1)

    def test_a_short_clip_squeezes_both_moves_in(self):
        keys = motion.plan(PRESETS["css-animate-rollIn-card"], 20, 24)
        for frames in (v for k, v in keys.items() if k != "moves"):
            self.assertGreaterEqual(min(frames), 0)
            self.assertLessEqual(max(frames), 19)

    def test_emphasis_at_the_playhead_or_mid_clip(self):
        tada = PRESETS["css-animate-tada-card"]
        at = motion.plan(tada, 240, 24, at=100)["moves"][0]
        self.assertEqual(at[1], 100)
        middle = motion.plan(tada, 240, 24)["moves"][0]
        self.assertAlmostEqual((middle[1] + middle[2]) / 2, 239 / 2, delta=1)
        late = motion.plan(tada, 240, 24, at=235)["moves"][0]
        self.assertLessEqual(late[2], 239)      # kept inside the clip

    def test_values_are_fusion_units(self):
        whip = values(motion.plan(PRESETS["whip"], 120, 24)["Center.X"])
        self.assertAlmostEqual(whip[0], -0.5)   # a whole frame to the left of centre
        swing = values(motion.plan(PRESETS["swing"], 120, 24)["Angle"])
        self.assertAlmostEqual(swing[0], 25)    # clockwise -25 in the data is Fusion's +25

    def test_view_has_the_timing_bar(self):
        v = motion.view(PRESETS["pop"])
        self.assertEqual(v["segs"][0][0], 0)
        self.assertEqual(v["segs"][-1][1], 1)


class Scaling(unittest.TestCase):
    def test_modes_from_the_clip_or_else_the_project(self):
        self.assertEqual(mr.scaling_mode(0, "scaleToFit"), mr.FIT)
        self.assertEqual(mr.scaling_mode(0, "scaleToCrop"), mr.FILL)
        self.assertEqual(mr.scaling_mode(0, "centerCrop"), mr.CROP)
        self.assertEqual(mr.scaling_mode(3, "scaleToFit"), mr.FILL)      # the clip's own wins
        self.assertEqual(mr.scaling_mode(None, None), mr.FIT)

    def test_fit_scale(self):
        self.assertAlmostEqual(mr.fit_scale(641, 479, 1920, 1080, mr.FIT), 1080 / 479)
        self.assertAlmostEqual(mr.fit_scale(641, 479, 1920, 1080, mr.FILL), 1920 / 641)
        self.assertEqual(mr.fit_scale(3840, 2160, 1920, 1080, mr.FIT), 0.5)
        self.assertEqual(mr.fit_scale(641, 479, 1920, 1080, mr.CROP), 1.0)
        self.assertEqual(mr.fit_scale(1920, 1080, 1920, 1080, mr.FIT), 1.0)


class OnClips(unittest.TestCase):
    def put(self, item, pid="css-animate-rollIn-card", clip_frames=120, at=None, canvas=(1920, 1080)):
        planned = []

        def plan_for(frames, ratio, a):
            planned.append((frames, ratio, a))
            return motion.plan(PRESETS[pid], frames, 24 * ratio, at=a)
        mr.apply(item, pid, plan_for, clip_frames, at, canvas, mr.FIT, mr.read_inspector(item), {"way": "both"})
        return planned

    def test_a_still_gets_a_comp_a_merge_and_the_round_trip(self):
        item = Item()
        self.put(item)
        comp = item.comps[0]
        self.assertEqual(chain(comp), ["MediaOut1", "BuddyMotion", "MediaIn1"])
        merge = comp.FindTool("BuddyMotion")
        self.assertEqual(merge.Background.source.tool.name, "BuddyMotionCanvas")
        self.assertEqual(comp.FindTool("BuddyMotionCanvas").inputs["TopLeftAlpha"].value, 0)
        self.assertTrue(curve(merge.Angle) and curve(merge.Blend))
        path = merge.Center.source.tool
        self.assertEqual(path.reg_id, "XYPath")
        self.assertTrue(curve(path.X))
        self.assertEqual(merge.GetData(mr.TAG), "css-animate-rollIn-card")
        self.assertEqual(item.reloads, 1)
        self.assertEqual(comp.locked, 0)

    def test_the_keys_go_on_the_spline_with_their_handles(self):
        item = Item()
        self.put(item, "slide")
        x = curve(item.comps[0].FindTool("BuddyMotion").Center.source.tool.X)
        self.assertEqual(len(x), len(motion.plan(PRESETS["slide"], 120, 24)["Center.X"]))
        first = x[min(x)]
        self.assertEqual(set(first), {1, "RH"})
        self.assertEqual(set(first["RH"]), {1, 2})

    def test_the_canvas_is_the_timelines_and_the_clip_is_fitted_to_it(self):
        still = Item(size=(641, 479))
        self.put(still, "css-animate-rollIn-card")      # no Size keys
        comp = still.comps[0]
        canvas = comp.FindTool("BuddyMotionCanvas")
        self.assertEqual((canvas.Width.value, canvas.Height.value), (1920, 1080))
        self.assertEqual(canvas.UseFrameFormatSettings.value, 0)
        self.assertAlmostEqual(comp.FindTool("BuddyMotion").Size.value, 1080 / 479)

        big = Item(size=(3840, 2160))
        self.put(big, "pop")                            # Size keys, and their handles, halved
        size = curve(big.comps[0].FindTool("BuddyMotion").Size)
        peak = max(size.values(), key=lambda k: k[1])
        self.assertAlmostEqual(peak[1], 1.22 * 0.5)
        plain = motion.plan(PRESETS["pop"], 120, 24)["Size"]
        frame = next(f for f, k in plain.items() if k["lh"])
        lh = plain[frame]["lh"]
        # A handle goes to Fusion as its offset from the key, heights halved too.
        self.assertAlmostEqual(size[frame]["LH"][1], lh[0] - frame)
        self.assertAlmostEqual(size[frame]["LH"][2], (lh[1] - plain[frame]["value"]) * 0.5)

    def test_a_text_plus_keeps_its_comp_and_its_template(self):
        item = Item(Comp(upstream="TextPlus"))
        self.put(item, "pop")
        self.assertEqual(chain(item.comps[0]), ["MediaOut1", "BuddyMotion", "TextPlus1"])
        self.assertIsNone(item.comps[0].FindTool("BuddyMotion").Size.value)   # already the timeline's size

    def test_keys_go_in_the_comps_own_frames(self):
        # 29.97 fps footage on a 24 fps timeline: a 56-frame clip, a 69-frame comp.
        item = Item(Comp(frames=69))
        planned = self.put(item, "pop", clip_frames=56, at=28)
        frames, ratio, at = planned[0]
        self.assertEqual(frames, 69)
        self.assertAlmostEqual(ratio, 69 / 56)
        self.assertAlmostEqual(at, 28 * 69 / 56)
        size = curve(item.comps[0].FindTool("BuddyMotion").Size)
        self.assertEqual(max(size), 68)         # the Out ends on the comp's last frame
        # ...and the In lasts as long on screen: 14 timeline frames.
        self.assertAlmostEqual(motion.plan(PRESETS["pop"], 69, 24 * 69 / 56)["moves"][0][2] / (69 / 56), 14, delta=1)

    def test_applying_again_replaces_instead_of_stacking(self):
        item = Item()
        self.put(item)
        self.put(item, "pop")
        comp = item.comps[0]
        self.assertEqual(len(comp.GetToolList(False, "Merge")), 1)
        self.assertEqual(len(comp.GetToolList(False, "Background")), 1)
        self.assertEqual(len(comp.GetToolList(False, "XYPath")), 0)     # Pop only scales
        self.assertEqual(len(comp.GetToolList(False, "BezierSpline")), 1)
        self.assertEqual(chain(comp)[-1], "MediaIn1")

    def test_remove_puts_a_users_comp_back(self):
        item = Item(Comp(upstream="TextPlus"))
        self.put(item)
        self.assertTrue(mr.remove(item))
        comp = item.comps[0]
        self.assertEqual(chain(comp), ["MediaOut1", "TextPlus1"])
        self.assertEqual(sorted(t.reg_id for t in comp.tools.values()), ["MediaOut", "TextPlus"])
        self.assertEqual(item.names, ["Composition 1"])     # each import replaced the one before

    def test_remove_leaves_a_comp_buddy_made_passing_straight_through(self):
        # Resolve won't delete a clip's last composition.
        item = Item()
        self.put(item, "pop")
        self.assertTrue(mr.remove(item))
        self.assertEqual(chain(item.comps[0]), ["MediaOut1", "MediaIn1"])
        self.assertEqual(item.names, ["Composition 1"])
        self.assertEqual(item.reloads, 2)

    def test_remove_without_a_preset_does_nothing(self):
        item = Item(Comp())
        self.assertFalse(mr.remove(item))
        self.assertFalse(mr.remove(Item()))

    def test_several_compositions_are_left_alone(self):
        item = Item(Comp())
        item.comps.append(Comp())
        item.names.append("Composition 2")
        with self.assertRaises(RuntimeError):
            self.put(item, "pop")

    def test_apply_again_and_again_on_the_comp_that_plays(self):
        # Each round trip's import is a new comp; the one it came from goes,
        # so the next Apply finds one comp, with the preset in it.
        item = Item()
        for pid in ("pop", "whip", "slide"):
            self.put(item, pid)
            self.assertEqual(item.names, ["Composition 1"])
        self.assertEqual(item.comps[0].FindTool("BuddyMotion").GetData(mr.TAG), "slide")
        self.assertTrue(mr._has_motion(item))
        self.assertTrue(mr.remove(item))
        self.assertFalse(mr._has_motion(item))
        self.assertEqual(len(item.comps), 1)

    def test_a_clip_buddy_1_1_36_left_two_comps_on(self):
        # The comp the preset was built in, and the imported copy that plays.
        item = Item()
        self.put(item, "pop")
        stale = copy.deepcopy(item.comps[0])
        item.comps.insert(0, stale)
        item.names = ["Composition 1", "Composition 2"]
        playing = item.comps[1]
        playing.FindTool("BuddyMotion").SetData(mr.TAG, "whip")
        self.assertEqual(mr._motion_comp(item)[1], playing)        # the newest, not comp 1
        self.put(item, "slide")                                     # no "several compositions"
        self.assertEqual(len(item.comps), 1)                        # both old ones gone
        self.assertEqual(item.comps[0].FindTool("BuddyMotion").GetData(mr.TAG), "slide")

    def test_a_delete_resolve_turns_down_leaves_a_comp_the_next_apply_passes_over(self):
        item = Item()
        self.put(item, "pop")
        item.refuse_delete = True
        self.put(item, "whip")
        self.assertEqual(len(item.comps), 2)
        item.refuse_delete = False
        self.put(item, "slide")
        self.assertEqual(len(item.comps), 1)
        self.assertEqual(item.comps[0].FindTool("BuddyMotion").GetData(mr.TAG), "slide")

    def test_a_failed_apply_leaves_the_clip_as_it_was(self):
        item = Item(Pan=40.0)
        self.put(item, "whip")
        before = chain(item.comps[0])
        props = dict(item.props)
        with mock.patch.object(mr, "_key", side_effect=RuntimeError("couldn't connect a keyframe curve")):
            with self.assertRaises(RuntimeError):
                self.put(item, "pop")
        self.assertEqual(len(item.comps), 1)
        comp = item.comps[0]
        self.assertEqual(comp.FindTool("BuddyMotion").GetData(mr.TAG), "whip")    # the old preset is still on
        self.assertEqual(chain(comp), before)
        self.assertTrue(curve(comp.FindTool("BuddyMotion").Center.source.tool.X))
        self.assertEqual(item.props, props)

    def test_a_failed_remove_leaves_the_preset_on(self):
        item = Item()
        self.put(item, "pop")
        with mock.patch.object(mr, "_take_out", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                mr.remove(item)
        self.assertEqual(len(item.comps), 1)
        self.assertEqual(item.comps[0].FindTool("BuddyMotion").GetData(mr.TAG), "pop")

    def test_a_one_frame_clip_is_turned_away_untouched(self):
        with self.assertRaises(ValueError):
            motion.plan(PRESETS["pop"], 1, 24)
        item = Item()
        with self.assertRaises(RuntimeError):
            self.put(item, "pop", clip_frames=1)
        self.assertEqual(item.comps, [])                            # no comp added for nothing
        two = motion.plan(PRESETS["pop"], 2, 24)["Size"]
        self.assertLessEqual(max(two), 1)                           # nothing past the last frame


def play(expression, time, start, end):
    """The comp frame a trim-following expression plays its keys at, worked out
    the way Fusion would: comp.RenderStart/End are the trimmed clip's."""
    mapping = expression.split('GetValue("Angle", ', 1)[1][:-1]
    mapping = mapping.replace("comp.RenderStart", "RS").replace("comp.RenderEnd", "RE")
    return eval(mapping, {"iif": lambda c, a, b: a if c else b, "time": time, "RS": start, "RE": end})


class Trimming(unittest.TestCase):
    """An In and an Out ride the clip's ends after it's trimmed in the Edit page.
    The ranges are what Studio 21.1 showed for a 5 s still: 0-119, 0-95 with a
    second off the end, 24-95 with a second off the start too."""

    def setUp(self):
        self.item = Item(name="still")
        mr.apply(self.item, "pop", lambda n, r, a: motion.plan(PRESETS["pop"], n, 24 * r, at=a), 120, None,
                 (1920, 1080), mr.FIT, mr.read_inspector(self.item), {"way": "both"})
        self.merge = self.item.comps[0].FindTool("BuddyMotion")
        self.expr = self.merge.Size.expression
        self.keys = curve(self.merge.Size)
        self.size = lambda c: motion.evaluate({f: {"value": k[1], "lh": None, "rh": None} for f, k in self.keys.items()}, c)

    def test_untrimmed_it_plays_as_laid_out(self):
        for t in range(120):
            self.assertAlmostEqual(play(self.expr, t, 0, 119), t)

    def test_a_trimmed_end_brings_the_out_with_it(self):
        for back in range(8):                       # the Out's last frames land on the new end
            self.assertAlmostEqual(play(self.expr, 95 - back, 0, 95), 119 - back)
        for t in range(10):                         # the In is where it was
            self.assertAlmostEqual(play(self.expr, t, 0, 95), t)
        self.assertAlmostEqual(self.size(play(self.expr, 95, 0, 95)), 0.0)   # gone by the last frame, as before

    def test_a_trimmed_start_brings_the_in_with_it(self):
        for k in range(10):
            self.assertAlmostEqual(play(self.expr, 24 + k, 24, 95), k)
        self.assertEqual(self.size(play(self.expr, 24, 24, 95)), 0.0)   # starts from nothing, as before
        self.assertAlmostEqual(play(self.expr, 95, 24, 95), 119)

    def test_a_longer_clip_holds_still_between_and_never_replays_the_in(self):
        in_end = next(last for name, _f, _l, _first, last in motion.plan(PRESETS["pop"], 120, 24)["moves"] if name == "in")
        for t in range(int(in_end) + 1, 288):                      # the Out starts at 288.92
            c = play(self.expr, t, 0, 299)
            self.assertGreaterEqual(c, in_end)
            self.assertAlmostEqual(self.size(c), 1.0, places=3)
        self.assertAlmostEqual(play(self.expr, 299, 0, 299), 119)

    def test_a_clip_trimmed_shorter_than_its_moves_squeezes_both(self):
        frames = [play(self.expr, t, 40, 49) for t in range(40, 50)]
        self.assertAlmostEqual(frames[0], 0)
        self.assertAlmostEqual(frames[-1], 119)
        self.assertEqual(frames, sorted(frames))
        self.assertAlmostEqual(play(self.expr, 40, 40, 40), 0)           # a single frame: no division by nothing

    def test_only_what_read_back_right_goes_in_the_expression(self):
        # time, comp.RenderStart/End, iif, numbers and Tool:GetValue - nothing
        # untried (an expression on a point input crashed Resolve 21.1).
        words = set(re.findall(r"[A-Za-z_][A-Za-z_.]*", self.expr))
        self.assertLessEqual(words, {"BuddyMotionKeysSize:GetValue", "BuddyMotionKeysSize", "GetValue", "Angle",
                                     "iif", "time", "comp.RenderStart", "comp.RenderEnd"})
        self.assertNotIn("e+", self.expr)

    def test_the_keys_sit_on_carriers_off_the_flow(self):
        comp = self.item.comps[0]
        self.assertEqual(chain(comp), ["MediaOut1", "BuddyMotion", "MediaIn1"])
        carrier = comp.FindTool("BuddyMotionKeysSize")
        self.assertEqual(carrier.reg_id, "Transform")
        self.assertTrue(carrier.Angle.source)                   # the spline
        self.assertIsNone(self.merge.Size.source)               # the Merge only reads it

    def test_a_move_follows_the_trims_through_the_motion_path(self):
        item = Item()
        mr.apply(item, "whip", lambda n, r, a: motion.plan(PRESETS["whip"], n, 24 * r, at=a), 120, None,
                 (1920, 1080), mr.FIT, mr.read_inspector(item), {"way": "both"})
        path = item.comps[0].FindTool("BuddyMotion").Center.source.tool
        self.assertEqual(path.reg_id, "XYPath")                 # no expression on the point itself
        self.assertIsNone(item.comps[0].FindTool("BuddyMotion").Center.expression)
        for axis in ("X", "Y"):
            self.assertTrue(getattr(path, axis).expression.startswith(f"BuddyMotionKeys{axis}:"))
        self.assertAlmostEqual(play(path.X.expression, 95, 0, 95), 119)

    def test_applying_again_and_removing_leave_no_stray_carriers(self):
        comp = lambda: self.item.comps[0]
        mr.apply(self.item, "whip", lambda n, r, a: motion.plan(PRESETS["whip"], n, 24 * r, at=a), 120, None,
                 (1920, 1080), mr.FIT, mr.read_inspector(self.item), {"way": "both"})
        names = sorted(n for n in comp().tools if n.startswith(mr.CARRIER))
        self.assertEqual(names, ["BuddyMotionKeysX", "BuddyMotionKeysY"])          # Pop's Size carrier went
        self.assertTrue(mr.remove(self.item))
        self.assertEqual([n for n in comp().tools if n.startswith(mr.CARRIER)], [])
        self.assertEqual(sorted(t.reg_id for t in comp().tools.values()), ["MediaIn", "MediaOut"])

    def test_an_emphasis_stays_on_the_frames_it_went_on(self):
        item = Item()
        tada = "css-animate-tada-card"
        mr.apply(item, tada, lambda n, r, a: motion.plan(PRESETS[tada], n, 24 * r, at=a), 120, 30,
                 (1920, 1080), mr.FIT, mr.read_inspector(item), {"way": "both"})
        merge = item.comps[0].FindTool("BuddyMotion")
        keyed = [getattr(merge, n) for n in ("Size", "Angle") if getattr(merge, n).source or getattr(merge, n).expression]
        self.assertTrue(keyed)
        for inp in keyed:
            self.assertIsNone(inp.expression)
        self.assertIsNone(mr.time_map(0, 120, 0, 0))


class Framing(unittest.TestCase):
    """The Inspector's framing goes into the comp; the Inspector goes neutral."""

    def put(self, item, pid="pop"):
        mr.apply(item, pid, lambda n, r, a: motion.plan(PRESETS[pid], n, 24 * r, at=a), 120, None,
                 (1920, 1080), mr.FIT, mr.read_inspector(item), {"way": "both", "speed": 1.0})

    def test_the_framing_goes_in_and_the_inspector_goes_neutral(self):
        item = Item(ZoomX=0.5, ZoomY=0.5, Pan=-300.0, Tilt=120.0, RotationAngle=15.0)
        self.put(item, "whip")
        merge = item.comps[0].FindTool("BuddyMotion")
        self.assertAlmostEqual(merge.Size.value, 0.5)              # a 1920x1080 clip: fit 1 x zoom
        self.assertAlmostEqual(merge.Angle.value, 15.0)
        x = curve(merge.Center.source.tool.X)
        settled = max(f for f in x if f < 60)                       # where the In comes to rest
        self.assertAlmostEqual(x[settled][1], 0.5 - 300 / 1920)     # Whip lands where it was placed
        y = curve(merge.Center.source.tool.Y)
        self.assertAlmostEqual(y[min(y)][1], 0.5 + 120 / 1080)
        self.assertTrue(framing.is_neutral(item.props))
        self.assertEqual(item.props["ZoomX"], 1.0)

    def test_a_crop_comes_in_ahead_of_the_move(self):
        item = Item(CropLeft=100.0, CropTop=40.0)
        self.put(item)
        comp = item.comps[0]
        self.assertEqual(chain(comp), ["MediaOut1", "BuddyMotion", "BuddyMotionCrop", "MediaIn1"])
        crop = comp.FindTool("BuddyMotionCrop")
        self.assertEqual((crop.XOffset.value, crop.YOffset.value), (100.0, 0.0))
        self.assertEqual((crop.XSize.value, crop.YSize.value), (1820.0, 1040.0))
        self.assertEqual(item.props["CropLeft"], 0.0)

    def test_a_soft_crop_stays_in_the_inspector(self):
        item = Item(CropLeft=100.0, CropSoftness=8.0)
        self.put(item)
        self.assertIsNone(item.comps[0].FindTool("BuddyMotionCrop"))
        self.assertEqual(item.props["CropLeft"], 100.0)

    def test_uneven_zoom_and_flip_get_a_shape_step(self):
        item = Item(ZoomGang=False, ZoomX=0.25, ZoomY=0.5, FlipX=True)
        self.put(item)
        shape = item.comps[0].FindTool("BuddyMotionShape")
        self.assertEqual(shape.FlipVert.value, 1)
        self.assertAlmostEqual(shape.XSize.value, 0.5)
        self.assertAlmostEqual(shape.YSize.value, 1.0)

    def test_reframing_after_goes_on_top_and_remove_puts_it_all_back(self):
        item = Item(ZoomX=0.5, ZoomY=0.5, Pan=100.0)
        self.put(item)
        item.props.update(Pan=-50.0)                                # re-framed in the Inspector
        self.put(item)                                              # Update framing / Apply again
        merge = item.comps[0].FindTool("BuddyMotion")
        self.assertAlmostEqual(merge.Center.value[1], 0.5 + 50 / 1920)   # -50 + 100
        self.assertEqual(item.props["Pan"], 0.0)
        self.assertTrue(mr.remove(item))
        self.assertAlmostEqual(item.props["Pan"], 50.0)
        self.assertAlmostEqual(item.props["ZoomX"], 0.5)

    def test_remove_puts_the_exact_original_back(self):
        item = Item(ZoomX=0.8, ZoomY=0.8, Pan=33.0, AnchorPointX=12.5, RotationAngle=-7.0)
        self.put(item, "css-animate-rollIn-card")
        self.assertTrue(mr.remove(item))
        for key, want in (("ZoomX", 0.8), ("Pan", 33.0), ("AnchorPointX", 12.5), ("RotationAngle", -7.0)):
            self.assertEqual(item.props[key], want)


class Shapes(unittest.TestCase):
    def test_the_tiles_follow_the_selection(self):
        self.assertEqual(mr.preview_shape({}), "card")
        self.assertEqual(mr.preview_shape({mr.NOT_VIDEO: 3}), "card")
        self.assertEqual(mr.preview_shape({mr.IMAGE: 1}), "photo")
        self.assertEqual(mr.preview_shape({mr.TITLE: 2, mr.IMAGE: 1}), "title")
        self.assertEqual(mr.preview_shape({mr.VIDEO: 1, mr.OTHER: 1}), "clip")
        self.assertEqual(mr.preview_shape({mr.IMAGE: 1, mr.VIDEO: 1}), "photo")   # a tie: the first


if __name__ == "__main__":
    unittest.main()
