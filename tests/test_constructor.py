"""Проверки конструктора без ComfyUI: python3 -m unittest discover tests"""
import contextlib
import importlib.util
import io
import os
import sys
import types
import unittest

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.modules.setdefault("folder_paths", types.ModuleType("folder_paths"))
spec = importlib.util.spec_from_file_location("ctd", os.path.join(PKG, "__init__.py"), submodule_search_locations=[PKG])
ctd = importlib.util.module_from_spec(spec)
sys.modules["ctd"] = ctd
spec.loader.exec_module(ctd)
cn, lr, safety = sys.modules["ctd.constructor"], sys.modules["ctd.lora_random"], sys.modules["ctd.safety"]
NODES = ctd.NODE_CLASS_MAPPINGS


def lora(stem, role, male=False, nsfw=False, trained=()):
    rel = f"Illustrious/{role}/{stem}.safetensors"
    male = male or lr._is_male(None, rel, role)
    return dict(rel=rel, stem=stem, family="illu", role=role, nsfw=nsfw, male=male, trained=list(trained), rec=None,
                name=stem, src="test")


CATALOG = [lora("Style_A", "style", trained=["style a"]), lora("Outfit_B", "clothing", trained=["outfit b"]),
           lora("Detail_C", "detail"), lora("Vaginal_Tugging", "concept", trained=["tug"]),
           lora("Scene_D", "act", nsfw=True, trained=["scene d"]), lora("Scene_E", "sex", nsfw=True)]


def generate(seed, dialect="e621", family="illu", rating="NSFW", composition=cn.RANDOM, recipe="", negative="guard"):
    cn.decide = lambda c, m, r: (m if m in ("e621", "danbooru") else dialect, "test", family)
    cn.checkpoint_info = lambda c: (None, family, "")
    cn.lora_catalog = lr.lora_catalog = lambda: CATALOG
    character = {n: cn.RANDOM for n in cn.CHARACTER_FIELDS}
    scene = {n: cn.RANDOM for n in cn.SCENE_FIELDS}
    with contextlib.redirect_stdout(io.StringIO()):
        plan = NODES["PromptBase"]().run("ck", seed, rating, composition, "auto", recipe, negative)[0]
        plan = NODES["PromptCharacter"]().run(plan, 0.15, 2, **character)[0]
        plan = NODES["PromptScene"]().run(plan, 0.9, 0.5, 0.15, 2, **scene)[0]
        plan = NODES["PromptLoras"]().run(plan, 10, 0.6, 3.5, 0.35, "", "")[0]
        result = NODES["PromptResult"]().run(plan, True)["result"]
    return dict(text=result[0], negative=result[1], recipe=result[3], clean=result[4], plan=plan)


def tags(text):
    return [t.strip() for t in text.split(",") if t.strip()]


class Safety(unittest.TestCase):
    MATRIX = [("e621", "illu"), ("danbooru", "illu"), ("e621", "pony"), ("e621", "sdxl")]

    def test_no_stop_tags_and_guard_always_present(self):
        for dialect, family in self.MATRIX:
            for rating in ("NSFW", "SFW"):
                for seed in range(300):
                    p = generate(seed, dialect, family, rating, negative="")
                    self.assertFalse([t for t in tags(p["clean"]) if safety.is_stopped(t)], p["clean"])
                    for word in tags(safety.AGE_GUARD):
                        self.assertIn(word, tags(p["negative"]))

    def test_stop_tags_cannot_pass_through_recipe(self):
        p = generate(1, recipe="Сцена: sex, tentacles; Вид: feral wolf; Волосы: long hair")
        self.assertNotIn("tentacles", p["clean"])
        self.assertNotIn("feral", p["clean"])
        self.assertIn("long hair", p["clean"])

    def test_result_has_no_string_input_to_bypass(self):
        """При обходе ноды ComfyUI подаёт на выход первый вход того же типа: строкового входа быть не должно."""
        required = NODES["PromptResult"].INPUT_TYPES()["required"]
        self.assertFalse([k for k, v in required.items() if v[0] == "STRING"])

    def test_no_distress_mood_in_partner_scenes(self):
        banned = set(cn.partner_ban(cn.load_tags()))
        for seed in range(1500):
            plan = generate(seed)["plan"]
            if plan["composition"] != "solo" and (plan["fields"]["Сцена"]["tags"] or plan.get("act")):
                self.assertFalse(banned & set(plan["fields"]["Эмоция"]["tags"]), plan["fields"]["Эмоция"])

    def test_no_youth_coded_tags_in_nsfw(self):
        excluded = set(cn.load_tags()["rating"]["NSFW"]["exclude"])
        for seed in range(1500):
            self.assertFalse(excluded & set(tags(generate(seed)["clean"])))


class Coherence(unittest.TestCase):
    def test_composition_follows_weights(self):
        n = 3000
        solo = sum(generate(s)["plan"]["composition"] == "solo" for s in range(n))
        self.assertTrue(0.65 < solo / n < 0.75)

    def test_horns_and_wings_only_on_fitting_species(self):
        data = cn.load_tags()
        groups = [g for g in data["fields"]["Особенности"]["groups"] if g.get("species_only")]
        for seed in range(2000):
            fields = generate(seed)["plan"]["fields"]
            species = fields["Вид"]["tags"][0].lower()
            for g in groups:
                if species not in g["species_only"]:
                    self.assertFalse(set(fields["Особенности"]["tags"]) & set(g["tags"]), species)

    def test_light_and_pose_fit_location(self):
        data = cn.load_tags()
        table = {l["tag"]: l for l in data["locations"]}
        for seed in range(2000):
            plan = generate(seed, composition="solo", rating="SFW")["plan"]
            place = plan["fields"]["Окружение"]["tags"]
            if place and place[0] in table:
                lights = {t for o in table[place[0]]["lights"] for t in cn.split_tags(o)}
                self.assertTrue(set(plan["fields"]["Свет"]["tags"]) <= lights, plan["fields"]["Свет"])

    def test_tags_data_is_consistent(self):
        data = cn.load_tags()
        places = {t for g in data["fields"]["Окружение"]["groups"] for t in g["tags"]}
        self.assertTrue({l["tag"] for l in data["locations"]} <= places)
        species = set(data["fields"]["Вид"]["groups"][0]["tags"])
        self.assertTrue(set(data["species_traits"]) <= species)


class Loras(unittest.TestCase):
    def test_partner_loras_need_a_man(self):
        for seed in range(2000):
            plan = generate(seed)["plan"]
            if plan["composition"] != "m/f":
                stems = {l["stem"] for l in plan["loras"]}
                self.assertFalse(stems & {"Vaginal_Tugging", "Scene_D", "Scene_E"}, plan["composition"])

    def test_plan_keeps_file_path(self):
        for seed in range(300):
            for l in generate(seed)["plan"]["loras"]:
                self.assertTrue(l["rel"])


class Recipe(unittest.TestCase):
    def test_recipe_repeats_any_seed(self):
        for seed in range(500):
            p = generate(seed)
            q = generate(seed + 100000, recipe=p["recipe"])
            self.assertEqual((p["text"], p["negative"]), (q["text"], q["negative"]))


class Presets(unittest.TestCase):
    def test_presets(self):
        self.assertEqual(cn.preset_for("Pony\\base\\ponyDiffusionV6XL_v6TurboMerge.safetensors")["steps"], 12)
        self.assertEqual(cn.preset_for("Pony\\base\\ponyDiffusionV6XL_v6StartWithThisOne.safetensors")["steps"], 25)
        self.assertEqual(cn.preset_for("unknown.safetensors")["cfg"], 5)

    def test_unsupported_checkpoint_gives_clear_error(self):
        with self.assertRaisesRegex(ValueError, "SDXL"):
            cn.check_supported("Anima\\furry\\molKeunMix_anima29B.safetensors")


if __name__ == "__main__":
    unittest.main()
