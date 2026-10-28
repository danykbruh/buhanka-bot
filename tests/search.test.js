// Поиск мини-аппа (webapp/search.js). Запуск из корня репозитория: node --test tests/
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const S = require("../webapp/search.js");
const menu = JSON.parse(fs.readFileSync(path.join(__dirname, "../webapp/menu.json"), "utf8"));
const search = S.createMenuSearch(menu);
const ids = (q) => search.search(q).map((r) => r.item.id);
const first = (q) => search.search(q)[0];

test("нормализация: регистр, ё, знаки и пробелы", () => {
  assert.equal(S.normalize("  Чай   ЧЁРНЫЙ!!, (300 мл) "), "чай черный 300 мл");
  assert.equal(S.normalize("По-аджарски"), "по аджарски");
});

test("Дамерау-Левенштейн: перестановка соседних букв - одна ошибка", () => {
  assert.equal(S.distance("латте", "латте"), 0);
  assert.equal(S.distance("круасан", "круассан"), 1);
  assert.equal(S.distance("капучнио", "капучино"), 1);
  assert.equal(S.distance("abc", "xyz", 1), 2); // ранний выход
});

test("допустимые ошибки по длине слова", () => {
  assert.deepEqual([3, 4, 5, 7, 8, 12].map(S.allowedErrors), [0, 0, 1, 1, 2, 2]);
});

test("транслитерация", () => {
  assert.equal(S.translit("kruassan"), "круассан");
  assert.equal(S.translit("cinnabon"), "синнабон");
  assert.equal(S.translit("hachapuri"), "хачапури");
  assert.equal(S.translit("пончик"), "пончик");
});

test('"капуч" - префикс, капучино первым', () => {
  const r = first("капуч");
  assert.equal(r.item.id, "cappuccino");
  assert.equal(r.kind, "prefix");
  assert.deepEqual(ids("капуч"), ["cappuccino"]); // капуста не находится
  assert.equal(first("капуччи").item.id, "cappuccino"); // опечатка в начале длинного запроса
});

test('"круасан" - опечатка, находит круассаны', () => {
  const found = ids("круасан");
  assert.ok(found.length >= 4);
  assert.ok(found.slice(0, 4).every((id) => id.startsWith("croissant-")), found.join());
  // и в однословном "круасан" нет точного или префиксного совпадения с самим словом
  assert.notEqual(first("круасан").kind, "exact");
});

test('"latte" - словарь, латте первым', () => {
  assert.equal(first("latte").item.id, "latte");
  assert.equal(first("latte").kind, "exact");
});

test('"kruassan" - транслитерация, находит круассаны', () => {
  assert.ok(ids("kruassan").slice(0, 4).every((id) => id.startsWith("croissant-")));
});

test('"cappuccino" и "КАПУЧИНО" - капучино первым', () => {
  assert.equal(first("cappuccino").item.id, "cappuccino");
  assert.equal(first("КАПУЧИНО").item.id, "cappuccino");
  assert.equal(first("КАПУЧИНО").kind, "exact");
  assert.equal(first("капучино!!").item.id, "cappuccino");
});

test('"булочка с корицей" - синнабон', () => {
  assert.deepEqual(ids("булочка с корицей"), ["cinnabon"]);
});

test("пустой запрос и бессмыслица - пусто", () => {
  assert.deepEqual(ids(""), []);
  assert.deepEqual(ids("   ,.! "), []);
  assert.deepEqual(ids("зщшгщывапр"), []);
  assert.deepEqual(ids("qwxzv"), []);
});

test("ранжирование: точное > префикс > синоним > опечатка", () => {
  // "латте": точное в названии латте выше, чем в составе комбо
  const latte = search.search("латте");
  assert.equal(latte[0].item.id, "latte");
  assert.ok(latte.some((r) => r.item.id === "combo-2"));
  // "кофе": кофейные напитки (подгруппа "Кофе") выше, чем комбо с кофе в составе
  assert.deepEqual(ids("кофе").slice(0, 4).sort(), ["americano", "cappuccino", "latte", "raf"]);
  // синоним ниже точного совпадения: "сладкое" - тег, "пончик" из синонимов тоже находится
  assert.ok(ids("сладкое").includes("donut-caramel"));
});

test("прежние сценарии: синонимы, окончания, ё, теги", () => {
  assert.ok(ids("пончики").every((id) => id.startsWith("donut-") || id.startsWith("combo-")));
  assert.ok(ids("пончики").includes("donut-chocolate"));
  assert.ok(ids("черный").includes("tea-black") && ids("чёрный").includes("tea-black"));
  assert.ok(ids("для детей").includes("juice-peach"));
  assert.ok(ids("лосось").includes("croissant-salmon"));
  assert.ok(ids("кофе").includes("raf"));
});

test("короткие слова без опечаток: до 4 букв только точно или по началу", () => {
  assert.ok(!ids("рах").includes("raf"));
  assert.ok(ids("раф").includes("raf"));
});

test("подсветка: позиции в исходном названии", () => {
  const mark = (title, q) => search.highlightRanges(title, q).map(([a, b]) => title.slice(a, b));
  assert.deepEqual(mark("Капучино", "капуч"), ["Капуч"]);
  assert.deepEqual(mark("Круассан с лососем", "круасан"), ["Круассан"]);
  assert.deepEqual(mark("Латте", "latte"), ["Латте"]);
  assert.deepEqual(mark("Чай чёрный", "черный"), ["чёрный"]);
  assert.deepEqual(mark("Латте", ""), []);
});
