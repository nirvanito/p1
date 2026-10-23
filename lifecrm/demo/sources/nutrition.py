"""Nutrition sample: what the persona ate, as Cronometer exports it.

Cronometer runs on both phones (Android and iPhone apps, one account), and
its export is the same pair of CSVs whichever device asks for it —
cronometer.com or the app: Settings > Account > Export Data, "Food & Recipe
Entries" (servings.csv) and "Daily Nutrition" (dailysummary.csv). Both
combos therefore get both files, identical for the same anchor.

The meals follow the rest of the story:

- the journal's meals: the chicken burrito bowl from Chipotle on day -40
  (the receipt is in the statements), fish tacos at Veracruz with Jake on
  day -38, the celebration dinner there on day -20, tacos again on day -1,
  and Maya's birthday omakase at Uchi on day -9;
- the ChatGPT meal-prep chat on day -26: from the next morning, Greek yogurt
  with oats for breakfast, chicken burrito bowls or turkey chili for lunch,
  turkey chili and chicken thighs for dinner, cottage cheese and a protein
  shake as snacks — bought on the H-E-B runs the statements show;
- each day's energy is the persona's `kcal_in` (persona.health_series), the
  figure Health Connect and Apple Health hold, so the food diary and the
  health apps agree on how much was eaten. Lunch and dinner each have one
  flexible item — the rice, the chili, the chips — sized so the day adds up;
  only the celebration dinner and the birthday omakase, whose fixed part is
  already a big meal, can come out a little over (and day -23 short).

Nutrient values are per USDA-style serving, rounded the way the app shows
them, and leave a deliberate pattern for `lifecrm nutrients` to find: low
vitamin D (salmon once a week is the only real source) and low magnesium
(white rice, little in the way of nuts or greens), borderline fiber, and
sodium over its 2,300 mg reference on the taco nights at Veracruz (and on
the Chipotle day, before the default 30-day window). Everything else the
report compares meets its reference. Biotin, chromium,
fluoride, iodine and molybdenum are left blank, as Cronometer leaves them
for most foods.

Day -23 (the short night, lunch with Priya) has no dinner logged and is not
marked Completed; today is logged through dinner but not marked Completed
either — the report skips both.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .. import persona

FILES = ("servings.csv", "dailysummary.csv")
PREP_FROM = -25                 # the morning after the ChatGPT meal-prep chat

# Cronometer's nutrient columns, in its export order: (short key, header).
COLUMNS: tuple[tuple[str, str], ...] = (
    ("kcal", "Energy (kcal)"), ("alc", "Alcohol (g)"), ("caf", "Caffeine (mg)"),
    ("water", "Water (g)"), ("b1", "B1 (Thiamine) (mg)"),
    ("b2", "B2 (Riboflavin) (mg)"), ("b3", "B3 (Niacin) (mg)"),
    ("b5", "B5 (Pantothenic Acid) (mg)"), ("b6", "B6 (Pyridoxine) (mg)"),
    ("b12", "B12 (Cobalamin) (µg)"), ("biotin", "Biotin (µg)"),
    ("choline", "Choline (mg)"), ("folate", "Folate (µg)"),
    ("va", "Vitamin A (µg)"), ("vc", "Vitamin C (mg)"), ("vd", "Vitamin D (IU)"),
    ("ve", "Vitamin E (mg)"), ("vk", "Vitamin K (µg)"), ("ca", "Calcium (mg)"),
    ("cr", "Chromium (µg)"), ("cu", "Copper (mg)"), ("fl", "Fluoride (µg)"),
    ("iodine", "Iodine (µg)"), ("fe", "Iron (mg)"), ("mg", "Magnesium (mg)"),
    ("mn", "Manganese (mg)"), ("mo", "Molybdenum (µg)"),
    ("p", "Phosphorus (mg)"), ("k", "Potassium (mg)"), ("se", "Selenium (µg)"),
    ("na", "Sodium (mg)"), ("zn", "Zinc (mg)"), ("carb", "Carbs (g)"),
    ("fib", "Fiber (g)"), ("fructose", "Fructose (g)"), ("galactose", "Galactose (g)"),
    ("glucose", "Glucose (g)"), ("lactose", "Lactose (g)"), ("maltose", "Maltose (g)"),
    ("starch", "Starch (g)"), ("sucrose", "Sucrose (g)"), ("sug", "Sugars (g)"),
    ("net", "Net Carbs (g)"), ("fat", "Fat (g)"), ("chol", "Cholesterol (mg)"),
    ("mono", "Monounsaturated (g)"), ("poly", "Polyunsaturated (g)"),
    ("sat", "Saturated (g)"), ("trans", "Trans-Fats (g)"), ("o3", "Omega-3 (g)"),
    ("o6", "Omega-6 (g)"), ("cys", "Cystine (g)"), ("his", "Histidine (g)"),
    ("ile", "Isoleucine (g)"), ("leu", "Leucine (g)"), ("lys", "Lysine (g)"),
    ("met", "Methionine (g)"), ("phe", "Phenylalanine (g)"), ("pro", "Protein (g)"),
    ("thr", "Threonine (g)"), ("trp", "Tryptophan (g)"), ("tyr", "Tyrosine (g)"),
    ("val", "Valine (g)"),
)
SERVINGS_HEADER = ["Day", "Time", "Group", "Food Name", "Amount",
                   *(h for _, h in COLUMNS), "Category"]
SUMMARY_HEADER = ["Date", *(h for _, h in COLUMNS), "Completed"]

MEALS = (("Breakfast", "08:05"), ("Lunch", "12:35"), ("Snacks", "15:30"),
         ("Dinner", "19:40"))
LUNCH_SHARE = 0.34              # of the day's energy, as the health samples split it


@dataclass(frozen=True)
class Food:
    name: str
    amount: float               # the base serving the values are for
    unit: str
    category: str
    n: dict                     # nutrient short key -> value per base serving


def _f(name, amount, unit, category, **n) -> Food:
    return Food(name, amount, unit, category, n)


DAIRY, GRAINS, FRUIT, VEG = ("Dairy and Egg Products", "Cereal Grains and Pasta",
                             "Fruits and Fruit Juices", "Vegetables and Vegetable Products")
MEAT, FISH, LEGUMES, DRINKS = ("Poultry Products", "Finfish and Shellfish Products",
                               "Legumes and Legume Products", "Beverages")
DISHES, SNACKS, FATS = ("Restaurant Foods", "Snacks", "Fats and Oils")

FOODS: dict[str, Food] = {f.name: f for f in (
    _f("Coffee, Brewed", 1, "cup", DRINKS, kcal=2.4, water=235, caf=95, b2=0.18,
       b3=0.5, b5=0.6, choline=6.2, folate=5, mg=7.1, mn=0.05, p=7.1, k=116, na=4.7,
       ca=4.7, pro=0.3),
    _f("Greek Yogurt, Plain, Nonfat", 1, "container (170 g)", DAIRY, kcal=100,
       water=145, b1=0.04, b2=0.47, b3=0.35, b5=0.56, b6=0.11, b12=1.3, choline=26,
       folate=12, va=2, ca=187, cu=0.03, fe=0.12, mg=19, mn=0.02, p=230, k=240,
       se=16.5, na=61, zn=0.9, carb=6.1, sug=5.5, fat=0.7, chol=8.5, sat=0.2,
       pro=17.3),
    _f("Instant Oatmeal, Plain, Fortified", 1, "packet (28 g)", GRAINS, kcal=101,
       water=2.4, b1=0.39, b2=0.33, b3=4.8, b6=0.48, choline=8.5, folate=101, va=297,
       ca=99, cu=0.11, fe=4.5, mg=22, mn=0.86, p=101, k=95, se=6.7, na=76, zn=0.86,
       carb=18.6, fib=2.7, sug=0.2, fat=1.9, mono=0.6, poly=0.7, sat=0.3, pro=3.6),
    _f("Oats, Rolled, Dry", 0.5, "cup", GRAINS, kcal=152, water=3.5, b1=0.3,
       b2=0.06, b3=0.4, b5=0.5, b6=0.05, choline=16, folate=13, ve=0.17, vk=0.8,
       ca=21, cu=0.16, fe=1.7, mg=55, mn=1.9, p=166, k=147, se=11.6, na=2, zn=1.5,
       carb=27.4, fib=4.1, sug=0.4, fat=2.6, mono=0.9, poly=1.0, sat=0.5, pro=5.3),
    _f("Banana", 1, "medium", FRUIT, kcal=105, water=88, b1=0.04, b2=0.09,
       b3=0.78, b5=0.39, b6=0.43, choline=11.6, folate=24, va=4, vc=10.3, ve=0.12,
       vk=0.6, ca=6, cu=0.09, fe=0.31, mg=32, mn=0.32, p=26, k=422, se=1.2, na=1,
       zn=0.18, carb=27, fib=3.1, sug=14.4, fat=0.4, pro=1.3),
    _f("Strawberries", 1, "cup", FRUIT, kcal=49, water=138, b6=0.07, folate=36,
       va=2, vc=89, ve=0.44, vk=3.3, ca=24, cu=0.07, fe=0.62, mg=20, mn=0.59, p=36,
       k=233, na=2, zn=0.21, carb=11.7, fib=3, sug=7.4, fat=0.5, pro=1),
    _f("Honey", 1, "tbsp", "Sweets", kcal=64, water=3.6, k=11, na=1, carb=17.3,
       sug=17.2, pro=0.1),
    _f("Egg, Whole, Scrambled", 1, "large", DAIRY, kcal=91, water=37.5, b2=0.22,
       b5=0.7, b6=0.09, b12=0.45, choline=147, folate=24, va=90, vd=44, ve=0.55,
       ca=28, fe=0.75, mg=6, p=98, k=66, se=15.5, na=88, zn=0.65, carb=0.8,
       sug=0.6, fat=6.7, chol=186, mono=2.6, poly=1.2, sat=2.0, pro=6.1),
    _f("Egg, Hard-Boiled", 1, "large", DAIRY, kcal=78, water=37.3, b2=0.26,
       b5=0.7, b6=0.06, b12=0.56, choline=147, folate=22, va=75, vd=44, ve=0.52,
       ca=25, fe=0.6, mg=5, p=86, k=63, se=15.4, na=62, zn=0.53, carb=0.6,
       sug=0.6, fat=5.3, chol=186, mono=2.0, poly=0.7, sat=1.6, pro=6.3),
    _f("Corn Tortilla", 1, "tortilla", GRAINS, kcal=57, water=12, b1=0.03,
       b3=0.4, folate=2, ca=21, fe=0.35, mg=18.5, mn=0.08, p=82, k=48, se=1.6,
       na=12, zn=0.35, carb=11.7, fib=1.6, fat=0.75, pro=1.5),
    _f("Avocado", 0.5, "fruit", FRUIT, kcal=114, water=49, b5=0.95, b6=0.17,
       choline=9.6, folate=61, va=5, vc=6.8, ve=1.4, vk=14.3, cu=0.13, fe=0.4,
       mg=19.5, mn=0.1, p=35, k=345, na=5, zn=0.43, carb=6, fib=4.6, sug=0.2,
       fat=10.5, mono=6.7, poly=1.2, sat=1.5, pro=1.3),
    _f("Pico de Gallo", 0.25, "cup", VEG, kcal=11, water=55, b6=0.05, folate=9,
       va=18, vc=9.6, vk=5.5, ca=9, fe=0.18, mg=5, k=160, na=160, carb=2.5,
       fib=0.8, sug=1.5, pro=0.5),
    _f("Milk, 2%", 1, "cup", DAIRY, kcal=122, water=218, b1=0.1, b2=0.45,
       b5=0.9, b12=1.3, choline=43, folate=12, va=134, vd=120, ca=293, mg=27,
       p=224, k=342, se=7.6, na=115, zn=1.2, carb=11.7, sug=12.3, fat=4.8,
       chol=20, sat=3.1, pro=8.1),
    _f("Chipotle, Chicken Burrito Bowl", 1, "bowl", DISHES, kcal=660, va=110,
       vc=24, vd=12, ca=330, fe=7.2, k=1080, na=2010, carb=75, fib=13, sug=5,
       fat=21, chol=140, sat=10.5, trans=0.5, pro=52),
    _f("Chipotle, Chips", 1, "bag", DISHES, kcal=540, ca=200, fe=1.4, k=270,
       na=390, carb=73, fib=7, sug=0.5, fat=25, sat=3.5, pro=7),
    _f("Chicken Thigh, Boneless, Roasted", 4, "oz", MEAT, kcal=203, water=72,
       b1=0.08, b2=0.27, b3=7.3, b5=1.4, b6=0.53, b12=0.4, choline=93, folate=9,
       va=23, vd=8, ve=0.33, vk=2.7, ca=12, cu=0.08, fe=1.3, mg=21, mn=0.02,
       p=227, k=300, se=27, na=107, zn=2.6, fat=9.3, chol=149, mono=3.9, poly=2.0,
       sat=2.6, pro=27.7),
    _f("Jasmine Rice, Enriched, Cooked", 1, "cup", GRAINS, kcal=205, water=108,
       b1=0.26, b2=0.02, b3=2.3, b5=0.62, b6=0.15, choline=3.3, folate=92, ca=16,
       cu=0.11, fe=1.9, mg=15, mn=0.75, p=68, k=55, se=11.9, na=2, zn=0.77,
       carb=44.5, fib=0.6, sug=0.1, fat=0.4, sat=0.1, pro=4.2),
    _f("Black Beans, Canned, Drained", 0.5, "cup", LEGUMES, kcal=109, water=88,
       b1=0.16, b2=0.05, b3=0.6, b6=0.06, choline=29, folate=72, ca=42, cu=0.2,
       fe=2.2, mg=42, mn=0.33, p=125, k=370, se=1.4, na=230, zn=0.8, carb=19.9,
       fib=8.3, sug=0.3, fat=0.4, pro=7.2),
    _f("Cheddar Cheese, Shredded", 0.25, "cup", DAIRY, kcal=114, water=10,
       b2=0.1, b12=0.23, choline=4.6, va=74, vd=6, ve=0.2, vk=0.8, ca=201,
       mg=8, p=128, k=21, se=3.9, na=176, zn=1.0, carb=0.4, sug=0.1, fat=9.4,
       chol=28, mono=2.6, poly=0.3, sat=5.3, pro=6.4),
    _f("Turkey Chili, Homemade", 1, "cup", DISHES, kcal=250, water=200, b1=0.2,
       b2=0.25, b3=6, b5=0.9, b6=0.55, b12=0.9, choline=95, folate=58, va=62,
       vc=13, ve=3.0, vk=8, ca=70, cu=0.32, fe=2.3, mg=16, mn=0.42, p=280, k=640,
       se=20, na=180, zn=3.2, carb=21, fib=4.4, sug=5.3, fat=8, chol=60, mono=3,
       poly=1.8, sat=2.1, pro=24),
    _f("Tortilla Chips", 1, "oz", SNACKS, kcal=140, water=0.6, b3=0.4, ve=1.4,
       folate=4, ca=30, fe=0.4, mg=16, p=60, k=55, na=120, zn=0.4, carb=18,
       fib=1.5, sug=0.2, fat=7, mono=1.9, poly=3.6, sat=1.0, pro=2),
    _f("Cottage Cheese, 2%", 0.5, "cup", DAIRY, kcal=92, water=92, b2=0.2,
       b12=0.5, choline=20, folate=14, va=25, ca=125, mg=10, p=168, k=141, se=11,
       na=348, zn=0.5, carb=4.6, sug=4.6, fat=2.6, chol=14, sat=1.6, pro=12),
    _f("Protein Shake, Chocolate", 1, "bottle (325 mL)", DRINKS, kcal=160,
       water=280, b1=0.45, b2=0.51, b3=6, b5=3, b6=0.6, b12=1.8, folate=120,
       va=270, vc=18, vd=40, ve=7.5, ca=500, fe=2.7, k=380, na=210, zn=3.3,
       carb=5, fib=1, sug=1, fat=3, chol=20, sat=1, pro=30),
    _f("Apple", 1, "medium", FRUIT, kcal=95, water=156, b6=0.07, folate=5, va=5,
       vc=8.4, ve=0.33, vk=4, ca=11, fe=0.22, mg=9, k=195, na=2, carb=25,
       fib=4.4, sug=19, fat=0.3, pro=0.5),
    _f("Salmon, Atlantic, Farmed, Baked", 5, "oz", FISH, kcal=291, water=91,
       b1=0.48, b2=0.2, b3=11.4, b5=2.1, b6=0.91, b12=3.9, choline=128, folate=48,
       va=82, vd=745, ve=1.6, vk=0.7, ca=21, cu=0.07, fe=0.48, mg=43, mn=0.02,
       p=360, k=540, se=58.8, na=86, zn=0.6, fat=17.5, chol=85, mono=6.3,
       poly=6.2, sat=3.5, o3=3.1, pro=31.4),
    _f("Broccoli, Steamed", 1, "cup", VEG, kcal=55, water=140, b1=0.1, b2=0.19,
       b3=0.86, b5=0.96, b6=0.31, choline=63, folate=168, va=120, vc=101, ve=2.3,
       vk=220, ca=62, cu=0.1, fe=1.0, mg=33, mn=0.3, p=105, k=457, se=2.5, na=64,
       zn=0.7, carb=11.2, fib=5.1, sug=2.2, fat=0.6, pro=3.7),
    _f("Roasted Potatoes", 1, "cup", VEG, kcal=200, water=105, b1=0.12,
       b3=2.2, b6=0.5, choline=22, folate=24, vc=14, ve=1.9, vk=10, ca=15, fe=1.2,
       mg=21, mn=0.25, p=90, k=640, na=120, zn=0.5, carb=29, fib=3.1, sug=1.5,
       fat=8, mono=5.5, poly=1, sat=1.2, pro=3.2),
    _f("Green Salad with Vinaigrette", 2, "cup", VEG, kcal=120, water=95,
       folate=82, va=280, vc=15, ve=4.2, vk=115, ca=50, fe=1.0, mg=10, k=300,
       na=120, carb=6, fib=2.2, sug=3, fat=10, mono=6.5, poly=1.4, sat=1.4,
       pro=1.5),
    _f("Spaghetti with Meat Sauce", 1.5, "cup", DISHES, kcal=420, water=210,
       b1=0.45, b2=0.3, b3=6, b6=0.4, b12=1.6, choline=60, folate=160, va=55,
       vc=12, ve=3.2, vk=10, ca=60, cu=0.3, fe=4.2, mg=38, mn=0.6, p=240, k=640,
       se=38, na=300, zn=4.5, carb=50, fib=4.5, sug=8, fat=13, chol=55, sat=4.6,
       pro=24),
    _f("Turkey Sandwich, Whole Wheat", 1, "sandwich", DISHES, kcal=360,
       water=120, b1=0.3, b3=7, b6=0.5, b12=0.5, choline=50, folate=60, va=30,
       vc=5, ve=0.6, vk=20, ca=120, fe=3.0, mg=40, p=280, k=420, se=35, na=700,
       zn=2, carb=38, fib=5, sug=6, fat=10, chol=45, sat=3, pro=27),
    _f("Chicken Caesar Wrap", 1, "wrap", DISHES, kcal=540, water=110, b3=9,
       b6=0.5, choline=80, folate=70, va=160, vc=10, ve=1.5, vk=70, ca=180,
       fe=3.4, mg=30, p=320, k=420, se=40, na=700, zn=2, carb=42, fib=3,
       sug=3, fat=26, chol=80, sat=7, pro=34),
    _f("Grilled Chicken Caesar Salad", 1, "salad", DISHES, kcal=480, water=260,
       b3=11, b6=0.7, b12=0.4, choline=110, folate=150, va=380, vc=22, ve=2.4,
       vk=140, ca=240, fe=2.4, mg=52, p=380, k=640, se=36, na=1050, zn=2.3,
       carb=14, fib=4, sug=3, fat=31, chol=110, sat=7, pro=40),
    _f("Sourdough Bread", 1, "slice", GRAINS, kcal=185, water=22, b1=0.33,
       b2=0.2, b3=3, folate=95, ca=30, fe=2.2, mg=17, p=67, k=75, se=19, na=410,
       zn=0.6, carb=36, fib=1.5, sug=1.5, fat=1.2, pro=7.5),
    _f("Fish Taco", 1, "taco", DISHES, kcal=245, water=80, b3=2.3, b6=0.2,
       b12=0.7, choline=42, folate=30, va=40, vc=8, vd=36, ve=1.2, vk=12, ca=80,
       fe=1.4, mg=30, p=180, k=280, se=16, na=640, zn=0.6, carb=24, fib=2.8,
       sug=2, fat=11, chol=35, sat=2.4, pro=13),
    _f("Migas Taco", 1, "taco", DISHES, kcal=290, water=70, b2=0.3, b12=0.6,
       choline=160, folate=40, va=120, vd=44, ve=1.0, ca=140, fe=1.8, mg=22,
       p=210, k=210, se=18, na=640, zn=1.2, carb=22, fib=2, sug=1.5, fat=17,
       chol=200, sat=6, pro=13),
    _f("Salsa Roja", 0.5, "cup", VEG, kcal=36, water=110, b6=0.15, folate=15,
       va=40, vc=12, vk=6, ca=30, fe=0.6, mg=15, k=300, na=760, carb=7, fib=2,
       sug=4, fat=0.2, pro=1.5),
    _f("Margarita", 1, "drink", DRINKS, kcal=274, water=180, alc=21, vc=3, k=40,
       na=120, carb=36, sug=32, pro=0.1),
    _f("Edamame, Unsalted", 0.5, "cup", LEGUMES, kcal=94, water=56, b1=0.15,
       b2=0.12, folate=241, vc=4.8, vk=20, ca=49, cu=0.27, fe=1.8, mg=50, mn=0.8,
       p=130, k=338, na=5, zn=1.0, carb=6.9, fib=4, sug=1.7, fat=4, pro=9.2),
    _f("Salmon Nigiri", 2, "piece", FISH, kcal=140, water=45, b3=3.4, b12=1.0,
       choline=28, vd=180, se=12, p=95, k=120, mg=10, na=120, carb=15, fat=4,
       chol=20, sat=0.9, o3=0.6, pro=9),
    _f("Hamachi Nigiri", 2, "piece", FISH, kcal=150, water=45, b3=3, b12=1.1,
       choline=25, vd=90, se=14, p=90, k=130, mg=12, na=120, carb=15, fat=5,
       chol=25, sat=1.2, o3=0.5, pro=9.5),
    _f("Tuna Roll", 6, "piece", FISH, kcal=200, water=80, b3=4, b12=0.9,
       choline=20, vd=30, se=30, fe=1.0, mg=20, p=110, k=180, na=300, carb=28,
       fib=1, fat=1.5, chol=15, pro=13),
    _f("Olive Oil", 1, "tbsp", FATS, kcal=119, ve=1.9, vk=8.1, fat=13.5, mono=9.9,
       poly=1.4, sat=1.9),
    _f("Sour Cream", 2, "tbsp", DAIRY, kcal=60, water=21, b2=0.04, b12=0.09,
       choline=5, va=29, ve=0.1, ca=30, mg=3, p=23, k=37, se=0.6, na=10, zn=0.1,
       carb=1.4, sug=1.1, fat=5.8, chol=17, mono=1.5, poly=0.2, sat=3.4, pro=0.7),
    _f("Sake", 1, "glass (180 mL)", DRINKS, kcal=230, water=140, alc=26, k=45,
       na=4, carb=8.1),
)}


# ---------------------------------------------------------------- menus

# Which home dinner, by day of a 7-day rotation (offset mod 7).
_HOME_DINNERS = {
    0: ([("Salmon, Atlantic, Farmed, Baked", 1), ("Broccoli, Steamed", 1),
         ("Olive Oil", 1)], "Jasmine Rice, Enriched, Cooked"),
    1: ([("Cheddar Cheese, Shredded", 1), ("Sour Cream", 1),
         ("Green Salad with Vinaigrette", 1)], "Turkey Chili, Homemade"),
    2: ([("Chicken Thigh, Boneless, Roasted", 1.5), ("Green Salad with Vinaigrette", 1),
         ("Olive Oil", 1)], "Roasted Potatoes"),
    3: ([("Green Salad with Vinaigrette", 1)], "Spaghetti with Meat Sauce"),
    4: ([("Cheddar Cheese, Shredded", 1), ("Sour Cream", 1), ("Avocado", 1)],
        "Turkey Chili, Homemade"),
    5: ([("Chicken Thigh, Boneless, Roasted", 1.5), ("Broccoli, Steamed", 1),
         ("Olive Oil", 1)], "Roasted Potatoes"),
    6: ([("Chicken Thigh, Boneless, Roasted", 1.25), ("Black Beans, Canned, Drained", 1),
         ("Pico de Gallo", 1), ("Green Salad with Vinaigrette", 1)],
        "Jasmine Rice, Enriched, Cooked"),
}
_BEFORE_PREP_DINNERS = {1: 3, 4: 2}           # no chili before the meal prep

# The dinners the rest of the story pins down (journal, receipts, texts).
_PINNED_DINNERS = {
    -38: ([("Fish Taco", 3), ("Salsa Roja", 1)], "Tortilla Chips"),
    -20: ([("Fish Taco", 2), ("Migas Taco", 1), ("Margarita", 1), ("Salsa Roja", 1)],
          "Tortilla Chips"),
    -9: ([("Edamame, Unsalted", 1), ("Salmon Nigiri", 2), ("Hamachi Nigiri", 2),
          ("Sake", 1)], "Tuna Roll"),
    -1: ([("Fish Taco", 2), ("Migas Taco", 1), ("Salsa Roja", 1)], "Tortilla Chips"),
}
INCOMPLETE_DAY = -23             # dinner never logged; not marked Completed


def _breakfast(off: int) -> list[tuple[str, float]]:
    if off >= PREP_FROM:
        fruit = "Banana" if off % 3 == 0 else "Strawberries"
        return [("Coffee, Brewed", 1), ("Greek Yogurt, Plain, Nonfat", 1.25),
                ("Instant Oatmeal, Plain, Fortified", 1.25), (fruit, 1), ("Honey", 1)]
    if off % 3 == 0:
        return [("Coffee, Brewed", 1), ("Egg, Whole, Scrambled", 2),
                ("Corn Tortilla", 2), ("Pico de Gallo", 1), ("Avocado", 1)]
    if off % 3 == 1:
        return [("Coffee, Brewed", 1), ("Milk, 2%", 1), ("Oats, Rolled, Dry", 1),
                ("Strawberries", 1), ("Honey", 1)]
    return [("Coffee, Brewed", 1), ("Greek Yogurt, Plain, Nonfat", 1),
            ("Oats, Rolled, Dry", 1), ("Banana", 1)]


def _lunch(off: int):
    if off == -40:
        return [("Chipotle, Chicken Burrito Bowl", 1)], "Chipotle, Chips"
    if off == INCOMPLETE_DAY:
        return [("Grilled Chicken Caesar Salad", 1)], "Sourdough Bread"
    if off >= PREP_FROM:
        if off % 2 == 0:
            return ([("Chicken Thigh, Boneless, Roasted", 1.25),
                     ("Black Beans, Canned, Drained", 0.75), ("Pico de Gallo", 1),
                     ("Cheddar Cheese, Shredded", 1)], "Jasmine Rice, Enriched, Cooked")
        return [("Tortilla Chips", 1), ("Sour Cream", 1)], "Turkey Chili, Homemade"
    if off % 2 == 0:
        return [("Turkey Sandwich, Whole Wheat", 1)], "Tortilla Chips"
    return [("Chicken Caesar Wrap", 1)], "Apple"


def _snack(off: int) -> list[tuple[str, float]]:
    if off < PREP_FROM:
        return [("Apple", 1)]
    if off % 2 == 0:
        return [("Protein Shake, Chocolate", 1), ("Egg, Hard-Boiled", 1)]
    return [("Cottage Cheese, 2%", 1), ("Egg, Hard-Boiled", 2)]


def _dinner(off: int):
    if off in _PINNED_DINNERS:
        return _PINNED_DINNERS[off]
    slot = off % 7
    if off < PREP_FROM:
        slot = _BEFORE_PREP_DINNERS.get(slot, slot)
    return _HOME_DINNERS[slot]


# ---------------------------------------------------------------- servings

def _round(v: float) -> float:
    return round(v + 0.0, 2)


def _serving(food: Food, factor: float, kcal: float | None = None) -> dict:
    """One logged serving: `factor` base servings. `kcal`, when given, is the
    energy the flexible item must carry so the meal adds up exactly."""
    values = {k: _round(v * factor) for k, v in food.n.items()}
    if kcal is not None:
        values["kcal"] = _round(kcal)
    if "carb" in values:
        values["net"] = _round(values["carb"] - values.get("fib", 0.0))
    return {"food": food.name, "amount": f"{food.amount * factor:.2f} {food.unit}",
            "category": food.category, "values": values}


def _meal(items, flex: str | None, target: float | None) -> list[dict]:
    """The fixed servings, then the flexible one sized to reach `target`
    kcal — within a quarter and four times its base serving; a meal its
    fixed part already overfills simply comes out larger."""
    out = [_serving(FOODS[name], factor) for name, factor in items]
    if flex is None:
        return out
    fixed = sum(s["values"]["kcal"] for s in out)
    food = FOODS[flex]
    want = (target - fixed) if target is not None else food.n["kcal"]
    factor = min(max(want / food.n["kcal"], 0.25), 4.0)
    exact = want if factor == want / food.n["kcal"] else None
    out.append(_serving(food, factor, exact))
    return out


def day_servings(off: int, kcal_in: int) -> list[tuple[str, str, dict]]:
    """[(group, 'HH:MM', serving)] for one day, its energy adding up to kcal_in
    wherever the lunch and dinner portions can absorb it."""
    logged: list[tuple[str, str, dict]] = []
    total = 0.0
    for group, hhmm in MEALS:
        if group == "Breakfast":
            items, flex = _breakfast(off), None
        elif group == "Lunch":
            items, flex = _lunch(off)
        elif group == "Snacks":
            items, flex = _snack(off), None
        else:
            if off == INCOMPLETE_DAY:
                continue
            items, flex = _dinner(off)
        if group == "Lunch":
            target = int(kcal_in * LUNCH_SHARE)
        elif group == "Dinner":
            target = kcal_in - total
        else:
            target = None
        meal = _meal(items, flex, target)
        total += sum(s["values"]["kcal"] for s in meal)
        logged += [(group, hhmm, s) for s in meal]
    return logged


def _num(v: float | None) -> str:
    return "" if v is None else f"{v:.2f}"


def _time_cell(hhmm: str) -> str:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return f"{(hh % 12) or 12}:{mm:02d} {'AM' if hh < 12 else 'PM'}"


def _check(combo: str) -> None:
    if combo not in persona.COMBOS:
        raise ValueError(f"no Cronometer export for combo '{combo}' "
                         f"(expected one of {', '.join(persona.COMBOS)})")


def write_samples(dest: Path, anchor: date, combo: str) -> list[Path]:
    """servings.csv and dailysummary.csv for the persona's six weeks."""
    _check(combo)
    dest.mkdir(parents=True, exist_ok=True)
    health = {h.date: h for h in persona.health_series(anchor)}
    servings = io.StringIO(newline="")
    summary = io.StringIO(newline="")
    sw = csv.writer(servings, lineterminator="\n")
    dw = csv.writer(summary, lineterminator="\n")
    sw.writerow(SERVINGS_HEADER)
    dw.writerow(SUMMARY_HEADER)
    for off in range(-persona.WINDOW_DAYS + 1, 1):
        d = persona.day(anchor, off)
        logged = day_servings(off, health[d].kcal_in)
        totals: dict[str, float] = {}
        for group, hhmm, s in logged:
            sw.writerow([d.isoformat(), _time_cell(hhmm), group, s["food"], s["amount"],
                         *(_num(s["values"].get(k)) for k, _ in COLUMNS), s["category"]])
            for k, v in s["values"].items():
                totals[k] = totals.get(k, 0.0) + v
        done = off != INCOMPLETE_DAY and off != 0
        dw.writerow([d.isoformat(), *(_num(_round(totals[k]) if k in totals else None)
                                      for k, _ in COLUMNS),
                     "true" if done else "false"])
    paths = []
    for fname, buf in zip(FILES, (servings, summary)):
        p = dest / fname
        p.write_text(buf.getvalue(), encoding="utf-8", newline="")
        paths.append(p)
    return paths
