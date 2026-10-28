/* Поиск по меню: опечатки, латиница, синонимы. Без зависимостей.
   В браузере доступен как window.BuhankaSearch, в Node (тесты) через require. */
(function (root) {
  "use strict";

  // Нижний регистр, ё -> е, знаки препинания и лишние пробелы убираются
  const normalize = (s) => String(s == null ? "" : s).toLowerCase().replace(/ё/g, "е")
    .replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  const words = (s) => normalize(s).split(" ").filter(Boolean);

  // Предлоги в запросе из нескольких слов ничего не уточняют: "булочка с корицей"
  const STOPWORDS = new Set(["с", "со", "и", "в", "во", "на", "по", "из", "для"]);

  // Ручной словарь латинских написаний. Значение - русское слово или его начало ("клубник" найдёт "клубникой")
  const DICTIONARY = {
    latte: "латте", late: "латте", latté: "латте",
    cappuccino: "капучино", capuccino: "капучино", cappucino: "капучино", capucino: "капучино",
    kapuchino: "капучино", cappuchino: "капучино",
    americano: "американо", amerikano: "американо", raf: "раф", espresso: "эспрессо", expresso: "эспрессо",
    coffee: "кофе", cofee: "кофе", kofe: "кофе", tea: "чай", chai: "чай", juice: "сок", sok: "сок",
    croissant: "круассан", croisant: "круассан", croissan: "круассан", kruassan: "круассан", kruasan: "круассан",
    cinnabon: "синнабон", sinnabon: "синнабон", donut: "пончик", doughnut: "пончик", ponchik: "пончик",
    syrnik: "сырник", syrniki: "сырник", sirniki: "сырник", cheesecake: "сырник",
    khachapuri: "хачапури", hachapuri: "хачапури", sandwich: "сэндвич", sendvich: "сэндвич",
    combo: "комбо", lunch: "ланч", pie: "пирож", puff: "слойк", sausage: "сосиск",
    chocolate: "шоколад", strawberry: "клубник", raspberry: "малин", cherry: "вишн", caramel: "карамел",
    almond: "миндал", salmon: "лосос", ham: "ветчин", cheese: "сыр", apple: "яблоч", peach: "персик",
    ginger: "имбир", milk: "молок", sweet: "сладкое", kids: "детск",
  };

  // Побуквенная транслитерация для слов, которых нет в словаре. Сначала длинные сочетания
  const TRANSLIT = [
    ["shch", "щ"], ["sch", "щ"], ["zh", "ж"], ["kh", "х"], ["ch", "ч"], ["sh", "ш"], ["ts", "ц"], ["tz", "ц"],
    ["yo", "е"], ["yu", "ю"], ["ya", "я"], ["ye", "е"], ["ck", "к"], ["ph", "ф"], ["th", "т"],
    ["a", "а"], ["b", "б"], ["c", "к"], ["d", "д"], ["e", "е"], ["f", "ф"], ["g", "г"], ["h", "х"], ["i", "и"],
    ["j", "дж"], ["k", "к"], ["l", "л"], ["m", "м"], ["n", "н"], ["o", "о"], ["p", "п"], ["q", "к"], ["r", "р"],
    ["s", "с"], ["t", "т"], ["u", "у"], ["v", "в"], ["w", "в"], ["x", "кс"], ["y", "ы"], ["z", "з"],
  ];
  const hasLatin = (s) => /[a-z]/.test(s);

  function translit(word) {
    if (!hasLatin(word)) return word;
    // c перед e, i, y читается как "с" (cinnabon), y после гласной как "й"
    let s = word.replace(/c(?=[eiy])/g, "s").replace(/([aeiou])y/g, "$1й");
    let out = "";
    for (let i = 0; i < s.length;) {
      const pair = TRANSLIT.find(([lat]) => s.startsWith(lat, i));
      if (pair) { out += pair[1]; i += pair[0].length; } else { out += s[i]; i += 1; }
    }
    return out;
  }

  // Расстояние Дамерау-Левенштейна (вариант с перестановкой соседних букв). Больше max - сразу max + 1
  function distance(a, b, max = Infinity) {
    if (Math.abs(a.length - b.length) > max) return max + 1;
    let prev2 = null, prev = Array.from({ length: b.length + 1 }, (_, j) => j);
    for (let i = 1; i <= a.length; i++) {
      const cur = [i];
      let rowMin = i;
      for (let j = 1; j <= b.length; j++) {
        const cost = a[i - 1] === b[j - 1] ? 0 : 1;
        let d = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
        if (prev2 && i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) d = Math.min(d, prev2[j - 2] + 1);
        cur.push(d);
        rowMin = Math.min(rowMin, d);
      }
      if (rowMin > max) return max + 1;
      prev2 = prev; prev = cur;
    }
    return prev[b.length];
  }

  // Сколько опечаток прощаем в слове: до 4 букв ни одной, 5-7 одну, от 8 две
  const allowedErrors = (len) => (len <= 4 ? 0 : len <= 7 ? 1 : 2);

  // Тип совпадения, от лучшего к худшему. Ранжирование: точное > префикс > синоним > опечатка
  const KIND = { exact: 4, prefix: 3, synonym: 2, fuzzy: 1 };

  // Варианты слова запроса: само слово, оно же без окончания, словарь и транслитерация для латиницы.
  // cap - лучший тип совпадения, который может дать вариант
  function variantsOf(token) {
    const out = [{ text: token, cap: KIND.exact, fuzzy: true }];
    if (hasLatin(token)) {
      if (DICTIONARY[token]) out.push({ text: DICTIONARY[token], cap: KIND.exact, fuzzy: true });
      if (token.length >= 3) {
        for (const [key, value] of Object.entries(DICTIONARY)) {
          if (key !== token && key.startsWith(token)) out.push({ text: value, cap: KIND.prefix, fuzzy: false });
        }
      }
      const t = translit(token);
      if (t !== token) out.push({ text: t, cap: KIND.exact, fuzzy: true });
    }
    // Окончания: "пончики" -> "пончик", "корицей" -> "кориц". Основа не короче 4 букв.
    // Такое совпадение чуть ниже настоящего префикса: "капучи" не должно уравнять капучино и капусту
    for (const v of out.slice()) {
      if (hasLatin(v.text) || v.text.length < 6) continue;
      for (let cut = 1; cut <= 3 && v.text.length - cut >= 4; cut++) out.push({ text: v.text.slice(0, -cut), cap: KIND.prefix, fuzzy: false, stem: cut });
    }
    return out;
  }

  // Лучшее совпадение варианта запроса со словом из меню или null.
  // kind - тип, penalty - штраф внутри типа (отрезанное окончание), len - сколько букв подсветить
  function matchWord(variants, word) {
    let best = null;
    const take = (kind, len, penalty = 0) => {
      if (!best || kind > best.kind || (kind === best.kind && penalty < best.penalty)) best = { kind, len, penalty };
    };
    for (const v of variants) {
      if (word === v.text) take(Math.min(KIND.exact, v.cap), word.length);
      else if (word.startsWith(v.text)) take(Math.min(KIND.prefix, v.cap), v.stem ? word.length : v.text.length, v.stem ? 2 * v.stem : 0);
      else if (v.fuzzy && !hasLatin(v.text)) {
        const max = allowedErrors(v.text.length);
        if (!max) continue;
        // Опечатка во всём слове ("круасан") или, для запроса от 6 букв, в его начале ("капуччи" при "капучино").
        // Короткое начало с ошибкой слишком многое находит: "капуч" совпало бы с "капус(той)"
        const head = v.text.length >= 6;
        const d = Math.min(distance(v.text, word, max),
          head && word.length > v.text.length ? distance(v.text, word.slice(0, v.text.length), max) : max + 1,
          head && word.length > v.text.length + 1 ? distance(v.text, word.slice(0, v.text.length + 1), max) : max + 1);
        if (d <= max) take(KIND.fuzzy, word.length);
      }
    }
    return best;
  }

  // Синонимы, которые подходят слову запроса: ключ совпал целиком, по началу (от 3 букв) или с опечаткой
  function synonymsOf(variants, synonyms) {
    const out = [];
    for (const [key, values] of synonyms) {
      const hit = variants.some((v) => v.text === key || (v.text.length >= 3 && key.startsWith(v.text))
        || (v.fuzzy && distance(v.text, key, allowedErrors(key.length)) <= allowedErrors(key.length)));
      if (hit) out.push(...values);
    }
    return out;
  }

  function queryTokens(query) {
    const all = words(query);
    const meaningful = all.filter((t) => !STOPWORDS.has(t));
    return meaningful.length ? meaningful : all;
  }

  /* items: позиции меню; fields(item) -> [{ text, bonus }] - где искать и насколько это важно;
     synonyms: { "кофе": ["латте", ...] } */
  function createSearch({ items, fields, synonyms = {} }) {
    const syn = Object.entries(synonyms).map(([k, vs]) => [normalize(k), vs.map(normalize)]);
    const index = items.map((item, order) => ({
      item, order,
      fields: fields(item).filter((f) => f.text).map((f) => ({ words: words(f.text), bonus: f.bonus || 0 })),
    }));

    function scoreToken(entry, token) {
      const variants = variantsOf(token);
      const synValues = synonymsOf(variants, syn);
      let best = 0, kind = 0;
      for (const f of entry.fields) {
        for (const w of f.words) {
          const m = matchWord(variants, w);
          let k = m ? m.kind : 0, score = m ? k * 10 + f.bonus - m.penalty : 0;
          if (k < KIND.synonym && synValues.some((v) => w.startsWith(v))) { k = KIND.synonym; score = k * 10 + f.bonus; }
          if (k && score > best) { best = score; kind = k; }
        }
      }
      return { score: best, kind };
    }

    // Результаты по убыванию релевантности, при равенстве - в порядке меню. Пустой запрос - пустой список
    function search(query) {
      const tokens = queryTokens(query);
      if (!tokens.length) return [];
      const found = [];
      for (const entry of index) {
        let score = 0, worst = KIND.exact;
        for (const t of tokens) {
          const r = scoreToken(entry, t);
          if (!r.score) { score = 0; break; }
          score += r.score;
          worst = Math.min(worst, r.kind);
        }
        if (score) found.push({ item: entry.item, score, kind: Object.keys(KIND).find((k) => KIND[k] === worst), order: entry.order });
      }
      return found.sort((a, b) => b.score - a.score || a.order - b.order);
    }

    // Что подсветить в названии: [[начало, конец], ...] в позициях исходной строки
    function highlightRanges(title, query) {
      const tokens = queryTokens(query);
      const ranges = [];
      if (!tokens.length) return ranges;
      const prepared = tokens.map((t) => { const v = variantsOf(t); return { v, syn: synonymsOf(v, syn) }; });
      for (const m of String(title).matchAll(/[\p{L}\p{N}]+/gu)) {
        const w = normalize(m[0]);
        let len = 0;
        for (const { v, syn: values } of prepared) {
          const hit = matchWord(v, w);
          if (hit) len = Math.max(len, hit.kind === KIND.prefix ? hit.len : w.length);
          else if (values.some((s) => w.startsWith(s))) len = w.length;
        }
        if (len) ranges.push([m.index, m.index + Math.min(len, m[0].length)]);
      }
      return ranges;
    }

    return { search, highlightRanges };
  }

  // Поиск по menu.json: название важнее всего, затем теги, подгруппа и категория, затем состав
  function createMenuSearch(menu) {
    const groupOf = new Map();
    (menu.categories || []).forEach((c) => (c.groups || []).forEach((g) => g.items.forEach((id) => groupOf.set(id, g.title))));
    const catOf = new Map((menu.categories || []).map((c) => [c.id, c]));
    return createSearch({
      items: menu.items,
      synonyms: menu.searchSynonyms || {},
      fields: (it) => {
        const cat = catOf.get(it.cat) || {};
        return [
          { text: it.title, bonus: 3 },
          { text: [...(it.tags || []), groupOf.get(it.id), cat.title, cat.tab].filter(Boolean).join(" "), bonus: 2 },
          { text: it.composition, bonus: 0 },
        ];
      },
    });
  }

  const api = { normalize, words, translit, distance, allowedErrors, createSearch, createMenuSearch, DICTIONARY };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BuhankaSearch = api;
})(typeof window !== "undefined" ? window : globalThis);
