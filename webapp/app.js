(async () => {
  "use strict";

  // ---------- Telegram ----------
  const tg = window.Telegram && window.Telegram.WebApp;
  const inTg = !!(tg && tg.platform && tg.platform !== "unknown");
  const tgAtLeast = (v) => !!(inTg && tg.isVersionAtLeast && tg.isVersionAtLeast(v));
  // sendData работает только при запуске из KeyboardButton. В этом случае initDataUnsafe пустой.
  const tgUser = inTg && tg.initDataUnsafe ? tg.initDataUnsafe.user : null;
  const canSendData = inTg && !tgUser;

  const haptic = {
    impact(style = "light") { if (tgAtLeast("6.1")) tg.HapticFeedback.impactOccurred(style); },
    select() { if (tgAtLeast("6.1")) tg.HapticFeedback.selectionChanged(); },
    notify(type) { if (tgAtLeast("6.1")) tg.HapticFeedback.notificationOccurred(type); },
  };

  // ---------- Данные ----------
  // Меню, цены и лимиты лежат в menu.json: тот же файл читает бот и пересчитывает по нему заказ
  async function loadMenu() {
    try {
      const res = await fetch("menu.json", { cache: "no-cache" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (!Array.isArray(data.items) || !data.items.length) throw new Error("пустое меню");
      return data;
    } catch (err) {
      console.error("Не удалось загрузить меню:", err);
      document.getElementById("sections").innerHTML = `<div class="empty">
        <div class="empty__icon" aria-hidden="true">🥐</div>
        <p class="empty__title">Не удалось загрузить меню</p>
        <p class="empty__text">Проверьте интернет и попробуйте ещё раз</p>
        <button class="primary-btn" type="button" onclick="location.reload()">Повторить</button>
      </div>`;
      document.getElementById("tabs").hidden = true;
      if (inTg) tg.ready();
      return null;
    }
  }

  const MENU = await loadMenu();
  if (!MENU) return;
  const { categories, items } = MENU;
  const BAKERY = MENU.bakery;
  const LIMITS = MENU.limits;
  // Слоты самовывоза считаются по времени пекарни, а не устройства
  const TZ_SHIFT = ((MENU.timezone && MENU.timezone.utcOffsetMinutes) || 0) * 60000;
  const byId = new Map(items.map((it) => [it.id, it]));
  const STORAGE_CART = "buhanka.cart.v1";
  const STORAGE_NAME = "buhanka.name.v1";
  const MAX_QTY = LIMITS.maxQty;
  const MAX_LINES = LIMITS.maxLines;

  // ---------- Утилиты ----------
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => `${n.toLocaleString("ru-RU")} ₽`;
  const norm = (s) => s.toLowerCase().replace(/ё/g, "е");
  const imgSrc = (id) => `img/menu/${id}.webp`;
  const pad = (n) => String(n).padStart(2, "0");
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, val) { try { localStorage.setItem(key, val); } catch { /* приватный режим */ } },
  };

  const ICON = {
    plus: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" d="M12 5v14M5 12h14"/></svg>',
    minus: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" d="M5 12h14"/></svg>',
    close: '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" d="M7 7l10 10M17 7 7 17"/></svg>',
  };

  // ---------- Опции и цены ----------
  const groupsOf = (item) => item.options || [];
  const defaultOpts = (item) => Object.fromEntries(groupsOf(item).map((g) => [g.id, g.default || null]));
  const needsChoice = (item) => groupsOf(item).some((g) => g.required && !g.default);
  const choiceOf = (group, id) => group.choices.find((c) => c.id === id);

  function unitPrice(item, opts) {
    return groupsOf(item).reduce((sum, g) => {
      const c = choiceOf(g, opts[g.id]);
      return sum + (c ? c.delta : 0);
    }, item.price);
  }

  function minPrice(item) {
    return groupsOf(item).reduce((sum, g) => sum + Math.min(...g.choices.map((c) => c.delta)), item.price);
  }

  function hasPriceRange(item) {
    return groupsOf(item).some((g) => new Set(g.choices.map((c) => c.delta)).size > 1);
  }

  function optsLabel(item, opts) {
    return groupsOf(item)
      .map((g) => choiceOf(g, opts[g.id]))
      .filter((c) => c && c.id !== "none")
      .map((c) => c.title)
      .join(", ");
  }

  function optsValid(item, opts) {
    return groupsOf(item).every((g) => !g.required || choiceOf(g, opts[g.id]));
  }

  // ---------- Вес, калорийность, состав комбо ----------
  const ALLERGEN_ORDER = MENU.allergens || [];
  const unitOf = (item) => (item.cat === "drinks" ? "мл" : "г");
  const sizeGroupOf = (item) => groupsOf(item).find((g) => g.id === "size");

  // Короткая строка на карточке в сетке
  function cardLine(item) {
    if (item.short) return item.short;
    const size = sizeGroupOf(item);
    if (size) return `${size.choices.map((c) => c.volume).join(" / ")} мл`;
    return item.weight ? `${item.weight} ${unitOf(item)}` : "";
  }

  // Из чего состоит комбо при выбранных опциях (без невыбранных и "без пончика")
  function partsOf(item, opts) {
    return (item.parts || [])
      .map((p) => {
        const part = byId.get(p.group ? opts[p.group] : p.id);
        if (!part) return null;
        return {
          item: part,
          title: part.title + (p.note ? `, ${p.note}` : ""),
          price: p.price ?? part.price,
          kcal: p.kcal ?? part.kcal,
          allergens: part.allergens || [],
        };
      })
      .filter(Boolean);
  }

  // Объём/вес и ккал с учётом выбранного объёма. Для комбо ккал - сумма частей, пока выбрано всё обязательное
  function statsOf(item, opts) {
    if (item.parts) {
      return { amount: "", kcal: optsValid(item, opts) ? partsOf(item, opts).reduce((s, p) => s + (p.kcal || 0), 0) : null };
    }
    const size = sizeGroupOf(item);
    const choice = size && choiceOf(size, opts.size);
    const amount = choice && choice.volume ? choice.volume : item.weight;
    const kcal = item.kcal == null ? null : Math.round((item.kcal * amount) / item.weight / 5) * 5;
    return { amount: amount ? `${amount} ${unitOf(item)}` : "", kcal };
  }

  function allergensOf(item, opts) {
    const set = new Set(item.parts ? partsOf(item, opts).flatMap((p) => p.allergens) : item.allergens || []);
    return ALLERGEN_ORDER.filter((a) => set.has(a));
  }

  // ---------- Корзина ----------
  const lineKey = (id, opts) => id + "|" + Object.keys(opts).sort().map((k) => `${k}=${opts[k]}`).join("&");

  let cart = loadCart();

  function loadCart() {
    try {
      const raw = JSON.parse(store.get(STORAGE_CART) || "[]");
      return raw
        .filter((l) => byId.has(l.id) && optsValid(byId.get(l.id), l.opts || {}) && l.qty > 0)
        .map((l) => ({ id: l.id, opts: l.opts || {}, qty: Math.min(MAX_QTY, l.qty | 0), key: lineKey(l.id, l.opts || {}) }))
        .slice(0, MAX_LINES);
    } catch {
      return [];
    }
  }

  function saveCart() {
    store.set(STORAGE_CART, JSON.stringify(cart.map(({ id, opts, qty }) => ({ id, opts, qty }))));
  }

  const cartTotal = () => cart.reduce((s, l) => s + unitPrice(byId.get(l.id), l.opts) * l.qty, 0);
  const cartCount = () => cart.reduce((s, l) => s + l.qty, 0);
  const itemQty = (id) => cart.reduce((s, l) => s + (l.id === id ? l.qty : 0), 0);
  const lastLineOf = (id) => [...cart].reverse().find((l) => l.id === id);

  function addToCart(id, opts, qty = 1) {
    const key = lineKey(id, opts);
    const line = cart.find((l) => l.key === key);
    if (line) line.qty = Math.min(MAX_QTY, line.qty + qty);
    else if (cart.length >= MAX_LINES) {
      haptic.notify("warning");
      toast(`В одном заказе не больше ${MAX_LINES} позиций`);
      return false;
    } else cart.push({ id, opts: { ...opts }, qty: Math.min(MAX_QTY, qty), key });
    cartChanged();
    return true;
  }

  function changeLine(key, delta) {
    const line = cart.find((l) => l.key === key);
    if (!line) return;
    line.qty = Math.min(MAX_QTY, line.qty + delta);
    if (line.qty <= 0) cart = cart.filter((l) => l !== line);
    cartChanged();
  }

  function cartChanged() {
    saveCart();
    refreshCards();
    if (top() === "cart") renderCart();
    if (top() === "checkout") renderSummary();
    syncMainButton();
    updateCartBadge();
  }

  // ---------- Каталог ----------
  const els = {
    tabs: $("#tabs"),
    sections: $("#sections"),
    search: $("#search"),
    searchClear: $("#searchClear"),
    searchEmpty: $("#searchEmpty"),
    product: $("#productScreen"),
    productBody: $("#productBody"),
    cart: $("#cartScreen"),
    cartBody: $("#cartBody"),
    cartClear: $("#cartClear"),
    checkout: $("#checkoutScreen"),
    slots: $("#slots"),
    slotsHint: $("#slotsHint"),
    name: $("#fieldName"),
    comment: $("#fieldComment"),
    summary: $("#checkoutSummary"),
    fallbackBar: $("#fallbackBar"),
    fallbackBtn: $("#fallbackBtn"),
    toast: $("#toast"),
    headerCart: $("#headerCart"),
    cartBadge: $("#cartBadge"),
    bakery: $("#bakeryScreen"),
    bakeryPick: $("#bakeryPick"),
    bakeryHint: $("#bakeryHint"),
    bakeryCities: $("#bakeryCities"),
    bakerySearch: $("#bakerySearch"),
    bakeryNear: $("#bakeryNear"),
    bakeryList: $("#bakeryList"),
  };

  let query = "";

  function ctrlHtml(item) {
    const qty = itemQty(item.id);
    if (!qty) {
      return `<button class="add-btn" type="button" data-act="add" aria-label="Добавить: ${esc(item.title)}">${ICON.plus}</button>`;
    }
    return `<div class="stepper">
      <button type="button" data-act="dec" aria-label="Убрать одну">${ICON.minus}</button>
      <span class="stepper__val" aria-live="polite">${qty}</span>
      <button type="button" data-act="inc" aria-label="Добавить ещё">${ICON.plus}</button>
    </div>`;
  }

  function cardHtml(item) {
    const price = hasPriceRange(item) ? `<small>от</small>${money(minPrice(item))}` : money(item.price);
    const meta = cardLine(item);
    return `<article class="card" data-id="${item.id}" tabindex="0" aria-label="${esc(item.title)}, ${esc(money(minPrice(item)))}">
      <div class="media">
        <img src="${imgSrc(item.id)}" alt="" loading="lazy" decoding="async" width="400" height="300">
        ${item.badge ? `<span class="badge">${esc(item.badge)}</span>` : ""}
      </div>
      <div class="card__body">
        <h3 class="card__title">${esc(item.title)}</h3>
        ${meta ? `<div class="card__meta">${esc(meta)}</div>` : ""}
        <div class="card__foot">
          <span class="price">${price}</span>
          <div class="card__ctrl" data-qty="${itemQty(item.id)}">${ctrlHtml(item)}</div>
        </div>
      </div>
    </article>`;
  }

  function renderTabs() {
    els.tabs.innerHTML = categories
      .map((c, i) => `<button class="tab${i === 0 ? " is-active" : ""}" type="button" data-cat="${c.id}">${esc(c.tab || c.title)}</button>`)
      .join("");
  }

  function renderSections() {
    const q = norm(query.trim());
    if (q) {
      const found = items.filter((it) => norm(`${it.title} ${it.desc} ${it.composition || ""} ${it.short || ""}`).includes(q));
      els.tabs.hidden = true;
      els.searchEmpty.hidden = found.length > 0;
      els.sections.innerHTML = found.length
        ? `<section class="section"><h2 class="section__title">Найдено: ${found.length}</h2><div class="grid">${found.map(cardHtml).join("")}</div></section>`
        : "";
    } else {
      els.tabs.hidden = false;
      els.searchEmpty.hidden = true;
      els.sections.innerHTML = categories
        .map((c) => {
          const list = items.filter((it) => it.cat === c.id);
          return `<section class="section" id="sec-${c.id}" data-cat="${c.id}">
            <h2 class="section__title">${esc(c.title)}</h2>
            <div class="grid">${list.map(cardHtml).join("")}</div>
          </section>`;
        })
        .join("");
    }
    markLoadedImages(els.sections);
    updateActiveTab();
  }

  function refreshCards() {
    $$(".card", els.sections).forEach((card) => {
      const ctrl = $(".card__ctrl", card);
      const qty = itemQty(card.dataset.id);
      if (String(qty) === ctrl.dataset.qty) return;
      const wasStepper = ctrl.dataset.qty !== "0";
      ctrl.dataset.qty = qty;
      if (qty && wasStepper) {
        $(".stepper__val", ctrl).textContent = qty; // без повторной анимации
      } else {
        ctrl.innerHTML = ctrlHtml(byId.get(card.dataset.id));
      }
    });
  }

  // Плавное появление фото и заглушка для битых картинок
  document.addEventListener("load", (e) => { if (e.target.tagName === "IMG") e.target.classList.add("is-loaded"); }, true);
  document.addEventListener("error", (e) => {
    if (e.target.tagName === "IMG") {
      e.target.classList.add("is-loaded");
      e.target.style.visibility = "hidden";
      if (e.target.parentElement) e.target.parentElement.classList.add("is-broken");
    }
  }, true);
  function markLoadedImages(root) {
    $$("img", root).forEach((img) => { if (img.complete && img.naturalWidth) img.classList.add("is-loaded"); });
  }

  els.sections.addEventListener("click", (e) => {
    const card = e.target.closest(".card");
    if (!card) return;
    const item = byId.get(card.dataset.id);
    const act = e.target.closest("[data-act]");
    if (!act) return openProduct(item.id);

    if (act.dataset.act === "add" || act.dataset.act === "inc") {
      const last = lastLineOf(item.id);
      if (last) changeLine(last.key, +1);
      else if (needsChoice(item)) return openProduct(item.id);
      else addToCart(item.id, defaultOpts(item));
      haptic.impact("light");
    } else if (act.dataset.act === "dec") {
      const last = lastLineOf(item.id);
      if (last) changeLine(last.key, -1);
      haptic.impact("light");
    }
  });

  els.sections.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.classList.contains("card")) {
      e.preventDefault();
      openProduct(e.target.dataset.id);
    }
  });

  // ---------- Поиск ----------
  els.search.addEventListener("input", () => {
    query = els.search.value;
    els.searchClear.hidden = !query;
    renderSections();
  });
  els.search.addEventListener("keydown", (e) => { if (e.key === "Enter") els.search.blur(); });
  els.searchClear.addEventListener("click", () => {
    els.search.value = "";
    query = "";
    els.searchClear.hidden = true;
    renderSections();
    els.search.focus();
  });

  // ---------- Вкладки: липкие, подсветка при прокрутке ----------
  let spyLockUntil = 0;

  els.tabs.addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    const sec = $(`#sec-${tab.dataset.cat}`);
    if (!sec) return;
    haptic.select();
    setActiveTab(tab.dataset.cat);
    spyLockUntil = Date.now() + 700;
    const y = sec.getBoundingClientRect().top + window.scrollY - els.tabs.offsetHeight + 1;
    window.scrollTo({ top: Math.max(0, y), behavior: "smooth" });
  });

  function setActiveTab(cat) {
    $$(".tab", els.tabs).forEach((t) => {
      const on = t.dataset.cat === cat;
      if (on && !t.classList.contains("is-active")) {
        const left = t.offsetLeft - (els.tabs.clientWidth - t.offsetWidth) / 2;
        els.tabs.scrollTo({ left, behavior: "smooth" });
      }
      t.classList.toggle("is-active", on);
    });
  }

  function updateActiveTab() {
    const tabsTop = els.tabs.getBoundingClientRect().top;
    els.tabs.classList.toggle("is-stuck", tabsTop <= 0 && window.scrollY > 0);
    if (query || Date.now() < spyLockUntil) return;
    const line = els.tabs.offsetHeight + 24;
    let current = categories[0].id;
    $$(".section[data-cat]", els.sections).forEach((s) => {
      if (s.getBoundingClientRect().top <= line) current = s.dataset.cat;
    });
    // Долистали до конца - активна последняя категория
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
      current = categories[categories.length - 1].id;
    }
    setActiveTab(current);
  }

  let scrollTicking = false;
  window.addEventListener("scroll", () => {
    if (scrollTicking) return;
    scrollTicking = true;
    requestAnimationFrame(() => { scrollTicking = false; updateActiveTab(); });
  }, { passive: true });

  // ---------- Навигация между экранами ----------
  const screens = { product: els.product, cart: els.cart, checkout: els.checkout, bakery: els.bakery };
  const stack = [];
  const top = () => stack[stack.length - 1] || "catalog";

  function open(name) {
    const el = screens[name];
    if (stack.includes(name)) return;
    stack.push(name);
    el.classList.remove("is-leaving");
    el.hidden = false;
    el.style.zIndex = 50 + stack.length;
    const scroller = $(".screen__scroll", el);
    if (scroller) scroller.scrollTop = 0;
    document.body.classList.add("is-locked");
    syncBackButton();
    syncMainButton();
  }

  function back() {
    const name = stack.pop();
    if (!name) return;
    const el = screens[name];
    if (document.activeElement && el.contains(document.activeElement)) document.activeElement.blur();
    el.classList.add("is-leaving");
    setTimeout(() => {
      if (!stack.includes(name)) { el.hidden = true; el.classList.remove("is-leaving"); }
    }, 190);
    if (!stack.length) document.body.classList.remove("is-locked");
    if (top() === "cart") renderCart();
    syncBackButton();
    syncMainButton();
  }

  function closeAll() {
    while (stack.length) back();
  }

  document.addEventListener("click", (e) => { if (e.target.closest("[data-back]")) back(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && stack.length) back(); });

  function syncBackButton() {
    if (!tgAtLeast("6.1")) return;
    if (stack.length) tg.BackButton.show();
    else tg.BackButton.hide();
  }

  // ---------- Главная кнопка (MainButton / запасная кнопка в браузере) ----------
  let mainAction = null;

  // Цвета палитры для системных элементов Telegram берутся из CSS-переменных
  function cssColor(name, fallback) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  }

  function setMain(text, action, active = true) {
    mainAction = text ? action : null;
    if (inTg) {
      const mb = tg.MainButton;
      if (!text) { mb.hide(); return; }
      mb.setParams({
        text,
        color: active ? cssColor("--accent", "#B5562B") : cssColor("--line", "#E8DCCB"),
        text_color: active ? cssColor("--on-accent", "#FFFFFF") : cssColor("--hint", "#7A6656"),
        is_active: active,
        is_visible: true,
      });
    } else {
      els.fallbackBar.hidden = !text;
      document.body.classList.toggle("has-fallback", !!text);
      if (text) {
        els.fallbackBtn.textContent = text;
        els.fallbackBtn.disabled = !active;
      }
    }
  }

  function runMain() {
    if (typeof mainAction === "function") mainAction();
  }

  function syncMainButton() {
    const total = cartTotal();
    switch (top()) {
      case "product":
        setMain(`Добавить - ${money(productTotal())}`, addFromProduct);
        break;
      case "cart":
        if (cart.length) setMain(`Оформить - ${money(total)}`, () => { haptic.impact("medium"); openCheckout(); });
        else setMain("");
        break;
      case "checkout":
        // Без выбранной пекарни кнопка неактивна
        if (cart.length) setMain(`Отправить заказ - ${money(total)}`, submitOrder, !!selectedBakery);
        else setMain("");
        break;
      default:
        if (cart.length) setMain(`Корзина (${cartCount()}) - ${money(total)}`, () => { haptic.impact("medium"); openCart(); });
        else setMain("");
    }
  }

  els.fallbackBtn.addEventListener("click", runMain);

  // ---------- Корзина в шапке ----------
  let badgeCount = cartCount();

  function updateCartBadge() {
    const count = cartCount();
    els.cartBadge.hidden = !count;
    els.cartBadge.textContent = count > 99 ? "99+" : count;
    els.headerCart.setAttribute("aria-label", count ? `Корзина, ${count} шт` : "Корзина");
    if (count > badgeCount) {
      els.cartBadge.classList.remove("is-bumped");
      void els.cartBadge.offsetWidth; // перезапуск анимации
      els.cartBadge.classList.add("is-bumped");
    }
    badgeCount = count;
  }

  els.headerCart.addEventListener("click", () => {
    haptic.impact("light");
    openCart();
  });

  // ---------- Экран товара ----------
  let product = null; // { item, opts, qty }

  const productTotal = () => (product ? unitPrice(product.item, product.opts) * product.qty : 0);

  function openProduct(id) {
    const item = byId.get(id);
    if (!item) return;
    product = { item, opts: defaultOpts(item), qty: 1 };
    renderProduct();
    open("product");
  }

  function optionGroupHtml(g) {
    const selected = product.opts[g.id];
    const req = g.required && !g.default ? `<span class="opt-group__req">обязательно</span>` : "";
    if (g.ui === "segment") {
      return `<div class="opt-group" data-group="${g.id}">
        <div class="opt-group__title">${esc(g.title)}</div>
        <div class="segment" role="radiogroup" aria-label="${esc(g.title)}">
          ${g.choices.map((c) => `<button type="button" role="radio" data-choice="${c.id}" aria-checked="${c.id === selected}">
            ${esc(c.title)}${c.delta ? `<small>+${money(c.delta)}</small>` : ""}
          </button>`).join("")}
        </div>
      </div>`;
    }
    return `<div class="opt-group" data-group="${g.id}">
      <div class="opt-group__title">${esc(g.title)} ${req}</div>
      <div class="radio-list" role="radiogroup" aria-label="${esc(g.title)}">
        ${g.choices.map((c) => `<button type="button" class="radio" role="radio" data-choice="${c.id}" aria-checked="${c.id === selected}">
          <span class="radio__mark"></span>
          <span class="radio__label">${esc(c.title)}</span>
          ${c.delta ? `<span class="radio__delta">+${money(c.delta)}</span>` : ""}
        </button>`).join("")}
      </div>
    </div>`;
  }

  function renderProduct() {
    const { item } = product;
    const pairs = (item.pairing || []).map((id) => byId.get(id)).filter(Boolean);
    els.productBody.innerHTML = `
      <div class="product__media">
        <img src="${imgSrc(item.id)}" alt="${esc(item.title)}" decoding="async" width="800" height="600">
        <button class="product__close" type="button" data-back aria-label="Закрыть">${ICON.close}</button>
      </div>
      <div class="product__content">
        <h2 class="product__title" id="productTitle">${esc(item.title)}</h2>
        <p class="product__stats" id="productStats"></p>
        <p class="product__desc">${esc(item.desc)}</p>
        ${groupsOf(item).map(optionGroupHtml).join("")}
        <div id="productParts"></div>
        ${item.composition ? `<div class="product__block">
          <h3 class="product__block-title">Состав</h3>
          <p class="product__composition">${esc(item.composition)}</p>
          <div id="productAllergens"></div>
        </div>` : ""}
        <div class="product__qty">
          <div>
            <div class="product__total-label">Сумма</div>
            <div class="product__total" id="productTotal">${money(productTotal())}</div>
          </div>
          <div class="stepper stepper--lg stepper--soft">
            <button type="button" data-step="-1" aria-label="Меньше">${ICON.minus}</button>
            <span class="stepper__val" id="productQty">${product.qty}</span>
            <button type="button" data-step="1" aria-label="Больше">${ICON.plus}</button>
          </div>
        </div>
      </div>
      ${pairs.length ? `<div class="pairing">
        <h3 class="product__block-title pairing__title">С этим берут</h3>
        <div class="pairing__list">${pairs.map(pairHtml).join("")}</div>
      </div>` : ""}
      <p class="product__note">Состав и КБЖУ ориентировочные</p>`;
    renderProductDetails();
  }

  function pairHtml(item) {
    return `<button type="button" class="pair" data-pair="${item.id}" aria-label="Добавить в корзину: ${esc(item.title)}">
      <span class="pair__media"><img src="${imgSrc(item.id)}" alt="" loading="lazy" decoding="async" width="200" height="150"></span>
      <span class="pair__title">${esc(item.title)}</span>
      <span class="pair__foot">
        <span class="pair__price">${money(minPrice(item))}</span>
        <span class="pair__add" data-pair-qty>${pairBadge(item)}</span>
      </span>
    </button>`;
  }

  const pairBadge = (item) => (itemQty(item.id) ? `<b>${itemQty(item.id)}</b>` : ICON.plus);

  // Части экрана товара, которые зависят от выбранных опций
  function renderProductDetails() {
    const { item, opts } = product;
    const { amount, kcal } = statsOf(item, opts);
    const inCart = itemQty(item.id);
    const stats = [amount, kcal != null ? `${kcal} ккал` : ""].filter(Boolean).join(" · ");
    $("#productStats").innerHTML = `${esc(stats)}${inCart ? `<span class="product__incart">в корзине: ${inCart}</span>` : ""}`;
    $("#productStats").hidden = !stats && !inCart;

    const allergensEl = $("#productAllergens");
    if (allergensEl) {
      const list = allergensOf(item, opts);
      allergensEl.innerHTML = list.length
        ? `<div class="allergens"><span class="allergens__label">Аллергены</span>${list.map((a) => `<span class="allergen">${esc(a)}</span>`).join("")}</div>`
        : "";
    }

    const partsEl = $("#productParts");
    if (!item.parts) { partsEl.innerHTML = ""; return; }
    const parts = partsOf(item, opts);
    const complete = optsValid(item, opts);
    const separately = parts.reduce((s, p) => s + p.price, 0);
    const save = separately - unitPrice(item, opts);
    partsEl.innerHTML = `<div class="combo">
      <h3 class="product__block-title">В наборе</h3>
      ${parts.map((p) => `<div class="combo__row"><span>${esc(p.title)}</span><span class="combo__price">${money(p.price)}</span></div>`).join("")}
      ${complete && save > 0
        ? `<div class="combo__sum"><span>Отдельно <s>${money(separately)}</s>, в комбо ${money(unitPrice(item, opts))}</span><span class="combo__save">Экономия ${money(save)}</span></div>`
        : `<div class="combo__sum combo__sum--hint">Выберите состав, и мы посчитаем выгоду</div>`}
    </div>`;
  }

  function updateProductTotals() {
    $("#productTotal").textContent = money(productTotal());
    $("#productQty").textContent = product.qty;
    renderProductDetails();
    syncMainButton();
  }

  els.productBody.addEventListener("click", (e) => {
    if (!product) return;
    const pair = e.target.closest("[data-pair]");
    if (pair) {
      const item = byId.get(pair.dataset.pair);
      if (needsChoice(item)) {
        // Без выбора опций добавить нельзя: показываем этот товар на том же экране
        product = { item, opts: defaultOpts(item), qty: 1 };
        renderProduct();
        els.productBody.scrollTo({ top: 0, behavior: "smooth" });
        syncMainButton();
        return;
      }
      if (!addToCart(item.id, defaultOpts(item))) return;
      haptic.impact("light");
      $("[data-pair-qty]", pair).innerHTML = pairBadge(item);
      toast(`${item.title} - в корзине`);
      return;
    }
    const choice = e.target.closest("[data-choice]");
    if (choice) {
      const groupEl = choice.closest("[data-group]");
      product.opts[groupEl.dataset.group] = choice.dataset.choice;
      $$("[data-choice]", groupEl).forEach((b) => b.setAttribute("aria-checked", String(b === choice)));
      groupEl.classList.remove("is-invalid");
      haptic.select();
      updateProductTotals();
      return;
    }
    const q = e.target.closest("[data-step]");
    if (q) {
      const next = Math.max(1, Math.min(MAX_QTY, product.qty + Number(q.dataset.step)));
      if (next !== product.qty) { product.qty = next; haptic.impact("light"); updateProductTotals(); }
    }
  });

  function addFromProduct() {
    const { item, opts, qty } = product;
    const missing = groupsOf(item).filter((g) => g.required && !choiceOf(g, opts[g.id]));
    if (missing.length) {
      missing.forEach((g) => {
        const el = $(`[data-group="${g.id}"]`, els.productBody);
        el.classList.remove("is-invalid");
        void el.offsetWidth; // перезапуск анимации
        el.classList.add("is-invalid");
      });
      $(`[data-group="${missing[0].id}"]`, els.productBody).scrollIntoView({ behavior: "smooth", block: "center" });
      haptic.notify("error");
      toast(`Выберите: ${missing.map((g) => g.title.toLowerCase().replace(/,.*$/, "")).join(", ")}`);
      return;
    }
    if (!addToCart(item.id, opts, qty)) return;
    haptic.notify("success");
    back();
    toast(`${item.title} - в корзине`);
  }

  // ---------- Корзина ----------
  function openCart() {
    renderCart();
    open("cart");
  }

  function renderCart() {
    els.cartClear.hidden = !cart.length;
    if (!cart.length) {
      els.cartBody.innerHTML = `<div class="empty">
        <div class="empty__icon" aria-hidden="true">🧺</div>
        <p class="empty__title">Корзина пуста</p>
        <p class="empty__text">Добавьте что-нибудь вкусное из меню</p>
        <button class="primary-btn" type="button" data-back>Вернуться в меню</button>
      </div>`;
      return;
    }
    els.cartBody.innerHTML = `
      <div class="lines">
        ${cart.map((l) => {
          const item = byId.get(l.id);
          const opts = optsLabel(item, l.opts);
          const unit = unitPrice(item, l.opts);
          return `<div class="line">
            <img class="line__img" src="${imgSrc(item.id)}" alt="" data-open="${item.id}" decoding="async">
            <div>
              <div class="line__title">${esc(item.title)}</div>
              ${opts ? `<div class="line__opts">${esc(opts)}</div>` : ""}
              <div class="line__foot">
                <div class="stepper stepper--soft">
                  <button type="button" data-line="${esc(l.key)}" data-delta="-1" aria-label="Меньше">${ICON.minus}</button>
                  <span class="stepper__val">${l.qty}</span>
                  <button type="button" data-line="${esc(l.key)}" data-delta="1" aria-label="Больше">${ICON.plus}</button>
                </div>
                <span class="line__sum">${money(unit * l.qty)}</span>
              </div>
            </div>
          </div>`;
        }).join("")}
      </div>
      <div class="cart-bakery">
        <div class="group__label">Где заберёте</div>
        <button type="button" class="panel bakery-pick${selectedBakery ? "" : " is-empty"}" data-pick-bakery>${bakeryPickHtml()}</button>
      </div>
      <div class="totals">
        <div class="totals__row totals__row--muted"><span>Позиций</span><span>${cartCount()} шт</span></div>
        <div class="totals__row totals__row--muted"><span>Самовывоз</span><span>бесплатно</span></div>
        <div class="totals__row totals__row--big"><span>Итого</span><span>${money(cartTotal())}</span></div>
      </div>
      <div class="cart-pad"></div>`;
  }

  els.cartBody.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-line]");
    if (btn) {
      changeLine(btn.dataset.line, Number(btn.dataset.delta));
      haptic.impact("light");
      return;
    }
    const img = e.target.closest("[data-open]");
    if (img) openProduct(img.dataset.open);
  });

  els.cartClear.addEventListener("click", () => {
    const doClear = () => { cart = []; cartChanged(); haptic.notify("warning"); };
    if (tgAtLeast("6.2")) tg.showConfirm("Очистить корзину?", (ok) => { if (ok) doClear(); });
    else if (window.confirm("Очистить корзину?")) doClear();
  });

  // ---------- Пекарни ----------
  const BAKERIES = window.BAKERIES || [];
  const bakeryById = new Map(BAKERIES.map((b) => [b.id, b]));
  const STORAGE_BAKERY = "buhanka.bakery.v1";
  const bakeryCities = Object.entries(BAKERIES.reduce((m, b) => ((m[b.city] = (m[b.city] || 0) + 1), m), {}))
    .sort((a, b) => b[1] - a[1])
    .map(([city, count]) => ({ city, count }));

  let selectedBakery = bakeryById.get(store.get(STORAGE_BAKERY)) || null;
  let bakeryCity = selectedBakery ? selectedBakery.city : (bakeryCities[0] || {}).city;
  let bakeryQuery = "";
  let userPos = null; // { lat, lng } после "Ближайшая ко мне"

  const ICON_PIN = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round" d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21Z"/><circle cx="12" cy="9.5" r="2.5" fill="none" stroke="currentColor" stroke-width="1.9"/></svg>';
  const ICON_CHECK = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" d="m5 12.5 4.5 4.5L19 7.5"/></svg>';
  const ICON_CHEVRON = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d="m9 5 7 7-7 7"/></svg>';

  // Адрес без города и административных округов: в списке город и так выбран
  function shortAddress(b) {
    const parts = b.address.split(",").map((s) => s.trim())
      .filter((p) => !/^(Москва|Санкт-Петербург|Московская обл(асть|\.))$/.test(p))
      .filter((p) => !/административный округ|городской округ| район$/.test(p));
    return parts.join(", ") || b.address;
  }

  function distanceKm(a, b) {
    const rad = (d) => (d * Math.PI) / 180;
    const dLat = rad(b.lat - a.lat);
    const dLng = rad(b.lng - a.lng);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2;
    return 6371 * 2 * Math.asin(Math.sqrt(h));
  }

  function formatDistance(km) {
    if (km < 1) return `${Math.max(10, Math.round((km * 1000) / 10) * 10)} м`;
    if (km < 10) return `${km.toFixed(1).replace(".", ",")} км`;
    return `${Math.round(km)} км`;
  }

  const bakeryDistance = (b) => (userPos ? formatDistance(distanceKm(userPos, b)) : "");

  function bakerySub(b, withCity) {
    return [withCity || bakeryCities.length > 1 ? b.city : "", b.metro ? `м. ${b.metro}` : "", b.hours || ""]
      .filter(Boolean).join(" · ");
  }

  // Плашка "Где заберёте" для корзины и оформления
  function bakeryPickHtml() {
    if (!selectedBakery) {
      return `<span class="bakery-pick__icon">${ICON_PIN}</span>
        <span class="bakery-pick__text">
          <span class="bakery-pick__addr">Выберите пекарню</span>
          <span class="bakery-pick__sub">${BAKERIES.length} пекарен, есть поиск и ближайшая к вам</span>
        </span>
        <span class="bakery-pick__go">${ICON_CHEVRON}</span>`;
    }
    const sub = [bakerySub(selectedBakery), bakeryDistance(selectedBakery)].filter(Boolean).join(" · ");
    return `<span class="bakery-pick__icon is-set">${ICON_PIN}</span>
      <span class="bakery-pick__text">
        <span class="bakery-pick__addr">${esc(shortAddress(selectedBakery))}</span>
        ${sub ? `<span class="bakery-pick__sub">${esc(sub)}</span>` : ""}
      </span>
      <span class="bakery-pick__action">Изменить</span>`;
  }

  function renderBakeryPick() {
    els.bakeryPick.innerHTML = bakeryPickHtml();
    els.bakeryPick.classList.toggle("is-empty", !selectedBakery);
    els.bakeryHint.hidden = !!selectedBakery;
  }

  function openBakery() {
    els.bakerySearch.value = bakeryQuery;
    renderBakeryCities();
    renderBakeryList();
    open("bakery");
  }

  function renderBakeryCities() {
    els.bakeryCities.hidden = bakeryCities.length < 2;
    els.bakeryCities.innerHTML = bakeryCities
      .map(({ city, count }) => `<button type="button" class="city" role="radio" data-city="${esc(city)}" aria-checked="${city === bakeryCity}">
        ${esc(city)} <small>${count}</small>
      </button>`)
      .join("");
  }

  function renderBakeryList() {
    const q = norm(bakeryQuery.trim());
    // С запросом ищем по всем городам: гость мог не переключить город
    let list = BAKERIES.filter((b) => (q ? norm(`${b.address} ${b.metro || ""}`).includes(q) : b.city === bakeryCity));
    list = userPos
      ? list.map((b) => [b, distanceKm(userPos, b)]).sort((a, b) => a[1] - b[1]).map(([b]) => b)
      : list.sort((a, b) => shortAddress(a).localeCompare(shortAddress(b), "ru"));
    els.bakeryList.innerHTML = list.length
      ? list.map((b) => {
          const sub = bakerySub(b, !!q);
          const dist = bakeryDistance(b);
          const on = selectedBakery && selectedBakery.id === b.id;
          return `<button type="button" class="bakery-row" role="radio" data-bakery="${b.id}" aria-checked="${!!on}">
            <span class="bakery-row__pin">${on ? ICON_CHECK : ICON_PIN}</span>
            <span class="bakery-row__text">
              <span class="bakery-row__addr">${esc(shortAddress(b))}</span>
              ${sub ? `<span class="bakery-row__sub">${esc(sub)}</span>` : ""}
            </span>
            ${dist ? `<span class="bakery-row__dist">${dist}</span>` : ""}
          </button>`;
        }).join("")
      : `<div class="empty empty--sm"><p class="empty__title">Не нашли такой адрес</p><p class="empty__text">Попробуйте название улицы без "ул." или номер дома</p></div>`;
  }

  function selectBakery(b) {
    selectedBakery = b;
    bakeryCity = b.city;
    store.set(STORAGE_BAKERY, b.id);
    if (tgAtLeast("6.9")) {
      try { tg.CloudStorage.setItem(STORAGE_BAKERY, b.id); } catch { /* нет CloudStorage */ }
    }
    bakeryChanged();
  }

  function bakeryChanged() {
    renderBakeryPick();
    if (stack.includes("checkout")) renderSlots();
    if (stack.includes("cart")) renderCart();
    syncMainButton();
  }

  // Геолокация: Telegram LocationManager (Bot API 8.0+), иначе браузерная
  function locate() {
    return new Promise((resolve, reject) => {
      const viaBrowser = () => {
        if (!navigator.geolocation) return reject("unsupported");
        navigator.geolocation.getCurrentPosition(
          (pos) => resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
          (err) => reject(err.code === 1 ? "denied" : "unavailable"),
          { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 },
        );
      };
      const lm = tgAtLeast("8.0") ? tg.LocationManager : null;
      if (!lm) return viaBrowser();
      const go = () => {
        if (!lm.isLocationAvailable) return viaBrowser();
        lm.getLocation((loc) => {
          if (loc) resolve({ lat: loc.latitude, lng: loc.longitude });
          else reject(lm.isAccessRequested && !lm.isAccessGranted ? "denied-tg" : "unavailable");
        });
      };
      if (lm.isInited) go();
      else lm.init(go);
    });
  }

  async function findNearest() {
    const btn = els.bakeryNear;
    if (btn.getAttribute("aria-busy") === "true") return;
    btn.setAttribute("aria-busy", "true");
    $("span", btn).textContent = "Определяем...";
    haptic.impact("light");
    try {
      userPos = await locate();
      const nearest = BAKERIES.reduce((best, b) => (!best || distanceKm(userPos, b) < distanceKm(userPos, best) ? b : best), null);
      if (nearest) bakeryCity = nearest.city;
      bakeryQuery = "";
      els.bakerySearch.value = "";
      renderBakeryCities();
      renderBakeryList();
      if (selectedBakery) renderBakeryPick();
      $("#bakeryScroll").scrollTo({ top: 0, behavior: "smooth" });
      haptic.notify("success");
      if (nearest) toast(`Ближайшая: ${shortAddress(nearest)}, ${bakeryDistance(nearest)}`);
    } catch (reason) {
      haptic.notify("error");
      if (reason === "denied-tg") {
        tg.showConfirm("Доступ к геолокации для мини-аппа выключен. Открыть настройки?", (ok) => {
          if (ok) tg.LocationManager.openSettings();
        });
      } else if (reason === "denied") {
        toast("Нет доступа к геолокации. Разрешите его в настройках");
      } else {
        toast("Не удалось определить местоположение");
      }
    } finally {
      btn.removeAttribute("aria-busy");
      $("span", btn).textContent = "Ближайшая ко мне";
    }
  }

  els.bakeryCities.addEventListener("click", (e) => {
    const b = e.target.closest("[data-city]");
    if (!b || b.dataset.city === bakeryCity) return;
    bakeryCity = b.dataset.city;
    bakeryQuery = "";
    els.bakerySearch.value = "";
    haptic.select();
    renderBakeryCities();
    renderBakeryList();
  });

  els.bakerySearch.addEventListener("input", () => {
    bakeryQuery = els.bakerySearch.value;
    renderBakeryList();
  });
  els.bakerySearch.addEventListener("keydown", (e) => { if (e.key === "Enter") els.bakerySearch.blur(); });
  els.bakeryNear.addEventListener("click", findNearest);

  els.bakeryList.addEventListener("click", (e) => {
    const row = e.target.closest("[data-bakery]");
    if (!row) return;
    selectBakery(bakeryById.get(row.dataset.bakery));
    haptic.select();
    renderBakeryList();
    setTimeout(() => { if (top() === "bakery") back(); }, 160);
  });

  document.addEventListener("click", (e) => { if (e.target.closest("[data-pick-bakery]")) openBakery(); });

  // ---------- Оформление ----------
  let slots = [];
  let selectedSlot = null;

  // Часы выбранной пекарни ("08:00-22:00"), если они есть в данных, иначе общие
  function currentHours() {
    const m = selectedBakery && /^(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})$/.exec(selectedBakery.hours || "");
    return m ? { open: m[1], close: m[2] } : BAKERY.hours;
  }

  // Время пекарни хранится в Date со сдвигом на её часовой пояс и читается через getUTC*.
  // Так слоты не зависят от часового пояса телефона и совпадают с проверкой в боте.
  function parseHM(hm, base) {
    const [h, m] = hm.split(":").map(Number);
    const d = new Date(base);
    d.setUTCHours(h, m, 0, 0);
    return d;
  }

  // Ближайшие слоты по 15 минут в часы работы. Если сегодня уже поздно - на завтра.
  // Тот же алгоритм повторён в bot.py (build_slots), менять их нужно вместе.
  function buildSlots(now = new Date(), hours = currentHours()) {
    const step = BAKERY.slotStep * 60000;
    const local = new Date(now.getTime() + TZ_SHIFT);
    const result = [];
    for (let dayOffset = 0; dayOffset < 2 && !result.length; dayOffset++) {
      const day = new Date(local);
      day.setUTCDate(day.getUTCDate() + dayOffset);
      const open = parseHM(hours.open, day);
      const last = new Date(parseHM(hours.close, day).getTime() - step);
      let t = dayOffset === 0 ? new Date(Math.ceil((local.getTime() + BAKERY.prepMinutes * 60000) / step) * step) : open;
      if (t < open) t = open;
      for (; t <= last && result.length < BAKERY.slotCount; t = new Date(t.getTime() + step)) {
        result.push({
          date: `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}`,
          time: `${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}`,
          day: dayOffset === 0 ? "сегодня" : "завтра",
          ts: t.getTime() - TZ_SHIFT, // настоящий момент времени
        });
      }
    }
    return result;
  }

  function renderSlots() {
    slots = buildSlots();
    if (!selectedSlot || !slots.some((s) => s.ts === selectedSlot.ts)) selectedSlot = slots[0] || null;
    els.slots.innerHTML = slots
      .map((s, i) => `<button type="button" class="slot" role="radio" data-slot="${i}" aria-checked="${selectedSlot && s.ts === selectedSlot.ts}">
        <span class="slot__time">${s.time}</span><span class="slot__day">${i === 0 ? "ближайшее" : s.day}</span>
      </button>`)
      .join("");
    const hours = currentHours();
    els.slotsHint.textContent = `Соберём заказ к выбранному времени. ${selectedBakery && selectedBakery.hours ? "Эта пекарня работает" : "Работаем"} с ${hours.open} до ${hours.close}.`;
  }

  els.slots.addEventListener("click", (e) => {
    const b = e.target.closest("[data-slot]");
    if (!b) return;
    selectedSlot = slots[Number(b.dataset.slot)];
    $$(".slot", els.slots).forEach((x) => x.setAttribute("aria-checked", String(x === b)));
    haptic.select();
  });

  function renderSummary() {
    els.summary.innerHTML =
      cart.map((l) => {
        const item = byId.get(l.id);
        const opts = optsLabel(item, l.opts);
        return `<div class="sum-row"><span>${esc(item.title)} × ${l.qty}${opts ? `<small>${esc(opts)}</small>` : ""}</span><b>${money(unitPrice(item, l.opts) * l.qty)}</b></div>`;
      }).join("") +
      `<div class="sum-row sum-row--total"><span>Итого</span><b>${money(cartTotal())}</b></div>`;
  }

  function openCheckout() {
    if (!els.name.value) {
      const tgName = tgUser ? [tgUser.first_name, tgUser.last_name].filter(Boolean).join(" ") : "";
      els.name.value = tgName || store.get(STORAGE_NAME) || "";
    }
    renderBakeryPick();
    renderSlots();
    renderSummary();
    open("checkout");
  }

  els.name.addEventListener("input", () => {
    els.name.closest(".group").classList.remove("is-invalid");
    $("#nameError").hidden = true;
  });

  let orderSent = false;

  function submitOrder() {
    if (orderSent) return;
    if (!selectedBakery) {
      haptic.notify("error");
      openBakery();
      return;
    }
    const name = els.name.value.trim();
    if (!name) {
      els.name.closest(".group").classList.add("is-invalid");
      $("#nameError").hidden = false;
      els.name.focus();
      haptic.notify("error");
      return;
    }
    // Слот мог устареть, пока экран был открыт
    if (!selectedSlot || selectedSlot.ts < Date.now() + 5 * 60000) {
      selectedSlot = null;
      renderSlots();
      haptic.notify("warning");
      toast("Время обновилось - выберите слот ещё раз");
      return;
    }

    // Бот пересчитывает заказ по menu.json: шлём id, выбранные опции и цены, которые видел гость
    const order = {
      v: 2,
      menu: MENU.version,
      items: cart.map((l) => ({ id: l.id, opts: l.opts, qty: l.qty, price: unitPrice(byId.get(l.id), l.opts) })),
      total: cartTotal(),
      pickup: { date: selectedSlot.date, time: selectedSlot.time },
      bakery: { id: selectedBakery.id, city: selectedBakery.city, address: selectedBakery.address },
      name: name.slice(0, LIMITS.nameMax),
      comment: els.comment.value.trim().slice(0, LIMITS.commentMax),
    };
    const json = JSON.stringify(order);
    if (new TextEncoder().encode(json).length > 4096) {
      haptic.notify("error");
      toast("Заказ слишком большой для одного сообщения - разделите его на два");
      return;
    }

    store.set(STORAGE_NAME, name);

    if (canSendData) {
      if (orderSent) return; // защита от двойного нажатия, пока Telegram закрывает мини-апп
      orderSent = true;
      setMain("Отправляем...", null, false);
      haptic.notify("success");
      cart = [];
      saveCart();
      tg.sendData(json); // Telegram закроет мини-апп и передаст данные боту
      return;
    }
    if (inTg) {
      // Открыто через меню бота или прямую ссылку: sendData здесь не работает
      haptic.notify("warning");
      tg.showAlert('Чтобы отправить заказ, откройте меню кнопкой "Открыть меню" под полем ввода в чате с ботом (команда /start).');
      return;
    }
    // Обычный браузер: демо-режим
    console.log("Заказ (демо, в Telegram уйдёт через sendData):", order);
    toast("Демо: заказ собран, JSON в консоли");
  }

  // ---------- Тост ----------
  let toastTimer = 0;
  function toast(text) {
    els.toast.textContent = text;
    els.toast.classList.add("is-shown");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => els.toast.classList.remove("is-shown"), 2200);
  }

  // ---------- Тема ----------
  function applyTheme() {
    if (!inTg) return;
    document.documentElement.dataset.theme = tg.colorScheme === "dark" ? "dark" : "light";
    const bg = cssColor("--bg", "#F6EFE4");
    try {
      // Произвольный цвет шапки поддерживается с 6.9, раньше только ключи темы
      if (tgAtLeast("6.9")) tg.setHeaderColor(bg);
      else if (tgAtLeast("6.1")) tg.setHeaderColor("bg_color");
      if (tgAtLeast("6.1")) tg.setBackgroundColor(bg);
      if (tgAtLeast("7.10")) tg.setBottomBarColor(bg);
    } catch { /* старые клиенты */ }
    syncMainButton(); // цвет акцента зависит от темы
  }

  // ---------- Старт ----------
  function init() {
    document.documentElement.classList.toggle("in-tg", inTg);
    els.name.maxLength = LIMITS.nameMax;
    els.comment.maxLength = LIMITS.commentMax;
    renderTabs();
    renderSections();

    if (inTg) {
      tg.ready();
      tg.expand();
      if (tgAtLeast("7.7")) tg.disableVerticalSwipes();
      tg.MainButton.onClick(runMain);
      if (tgAtLeast("6.1")) tg.BackButton.onClick(back);
      tg.onEvent("themeChanged", applyTheme);
      applyTheme();
      // Пекарня из облачного хранилища Telegram, если локально её нет (другое устройство)
      if (!selectedBakery && tgAtLeast("6.9")) {
        tg.CloudStorage.getItem(STORAGE_BAKERY, (err, id) => {
          if (!err && bakeryById.has(id) && !selectedBakery) {
            selectedBakery = bakeryById.get(id);
            bakeryCity = selectedBakery.city;
            store.set(STORAGE_BAKERY, id);
            bakeryChanged();
          }
        });
      }
    }
    syncMainButton();
    updateCartBadge();
  }

  // Для отладки в консоли
  window.__buhanka = { get cart() { return cart; }, buildSlots, closeAll, statsOf, partsOf, byId };

  init();
})();
