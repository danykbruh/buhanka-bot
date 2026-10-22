/*
 * Данные меню пекарни "Буханка" (концепт).
 * Логика мини-аппа в app.js, здесь только данные.
 *
 * TODO: все цены примерные (ориентир - московская пекарня, 2026). Сверить с актуальным меню.
 *
 * Поля позиции:
 *   id       - совпадает с именем фото: img/menu/<id>.webp
 *   cat      - id категории
 *   title    - название
 *   desc     - короткое описание
 *   meta     - вес / объём / состав мелким текстом
 *   price    - базовая цена, ₽
 *   badge    - необязательная плашка на фото
 *   tab      - (у категории) короткая подпись для вкладки
 *   options  - группы опций. Цена позиции = price + сумма delta выбранных вариантов.
 *              default - вариант по умолчанию. Если у обязательной группы нет default,
 *              кнопка "+" на карточке открывает экран товара, чтобы гость выбрал сам.
 */

const SIZE_COFFEE = (delta400) => ({
  id: "size",
  title: "Объём",
  ui: "segment",
  required: true,
  default: "300",
  choices: [
    { id: "300", title: "300 мл", delta: 0 },
    { id: "400", title: "400 мл", delta: delta400 },
  ],
});

window.BAKERY = {
  name: "Буханка",
  // Часы работы для слотов самовывоза (локальное время устройства)
  hours: { open: "08:00", close: "22:00" },
  // Минимальное время на сборку заказа, мин
  prepMinutes: 15,
  slotStep: 15,
  slotCount: 12,
};

window.MENU = {
  categories: [
    { id: "bakery", title: "Горячая выпечка", tab: "Выпечка" },
    { id: "drinks", title: "Напитки" },
    { id: "combo", title: "Комбо-завтраки", tab: "Комбо" },
  ],

  items: [
    // ---------- Горячая выпечка ----------
    { id: "croissant-classic", cat: "bakery", title: "Круассан классический", price: 190, meta: "70 г", badge: "Хит",
      desc: "Слоёное тесто на сливочном масле, хрустящая корочка и мягкие слои внутри." },
    { id: "croissant-salmon", cat: "bakery", title: "Круассан с лососем", price: 450, meta: "160 г",
      desc: "Слабосолёный лосось, сливочный сыр и свежий огурец в тёплом круассане." },
    { id: "croissant-raspberry", cat: "bakery", title: "Круассан с малиной", price: 260, meta: "110 г",
      desc: "Малиновый конфитюр внутри и сахарная пудра сверху." },
    { id: "croissant-almond", cat: "bakery", title: "Круассан миндальный", price: 290, meta: "120 г",
      desc: "С миндальным кремом франжипан и обжаренными лепестками миндаля." },
    { id: "cinnabon", cat: "bakery", title: "Синнабон", price: 290, meta: "140 г", badge: "Хит",
      desc: "Сдобная булочка с корицей и нежным сливочно-сырным кремом." },
    { id: "puff-cherry", cat: "bakery", title: "Слойка с вишней", price: 180, meta: "100 г",
      desc: "Хрустящая слойка с кисло-сладкой вишнёвой начинкой." },
    { id: "pie-cabbage", cat: "bakery", title: "Пирожок с капустой", price: 120, meta: "90 г",
      desc: "Печёный пирожок с тушёной капустой - как у бабушки." },
    { id: "khachapuri-mini", cat: "bakery", title: "Хачапури по-аджарски", price: 390, meta: "180 г",
      desc: "Лодочка из дрожжевого теста с сулугуни, яйцом и сливочным маслом." },
    { id: "sausage-roll", cat: "bakery", title: "Сосиска в тесте", price: 160, meta: "110 г",
      desc: "Молочная сосиска в мягком сдобном тесте." },
    { id: "donut-strawberry", cat: "bakery", title: "Пончик с клубникой", price: 170, meta: "80 г",
      desc: "Воздушный пончик в клубничной глазури." },
    { id: "donut-chocolate", cat: "bakery", title: "Пончик шоколадный", price: 170, meta: "80 г",
      desc: "Пончик в глазури из тёмного шоколада." },
    { id: "donut-caramel", cat: "bakery", title: "Пончик карамельный", price: 170, meta: "80 г",
      desc: "Пончик с солёной карамелью сверху." },
    { id: "sandwich-ham-cheese", cat: "bakery", title: "Сэндвич с ветчиной и сыром", price: 340, meta: "190 г",
      desc: "Ветчина, сыр, листья салата и горчичный соус на пшеничном хлебе." },
    { id: "syrnik", cat: "bakery", title: "Сырники", price: 320, meta: "3 шт, 180 г",
      desc: "Из фермерского творога, со сметаной и ягодным соусом." },

    // ---------- Напитки ----------
    { id: "americano", cat: "drinks", title: "Американо", price: 220, options: [SIZE_COFFEE(40)],
      desc: "Двойной эспрессо и горячая вода. Зерно средней обжарки." },
    { id: "cappuccino", cat: "drinks", title: "Капучино", price: 260, options: [SIZE_COFFEE(40)], badge: "Хит",
      desc: "Эспрессо с плотной молочной пеной." },
    { id: "latte", cat: "drinks", title: "Латте", price: 280, options: [SIZE_COFFEE(40)],
      desc: "Эспрессо и много нежного молока." },
    { id: "raf", cat: "drinks", title: "Раф", price: 320, options: [SIZE_COFFEE(40)],
      desc: "Эспрессо, взбитый со сливками и ванильным сахаром." },
    { id: "tea-black", cat: "drinks", title: "Чай чёрный", price: 180, options: [SIZE_COFFEE(30)],
      desc: "Крупнолистовой цейлонский чай." },
    { id: "tea-ginger", cat: "drinks", title: "Чай имбирный", price: 240, options: [SIZE_COFFEE(40)],
      desc: "Свежий имбирь, лимон и мёд. Согревает." },
    { id: "tea-seabuckthorn", cat: "drinks", title: "Чай облепиховый", price: 240, options: [SIZE_COFFEE(40)],
      desc: "Облепиха с апельсином и пряностями." },
    { id: "juice-apple", cat: "drinks", title: "Сок яблочный", price: 170, meta: "250 мл",
      desc: "Прямого отжима, без добавленного сахара." },
    { id: "juice-peach", cat: "drinks", title: "Сок персиковый", price: 170, meta: "250 мл",
      desc: "Нектар из спелых персиков." },
    { id: "juice-multifruit", cat: "drinks", title: "Сок мультифрукт", price: 170, meta: "250 мл",
      desc: "Яблоко, апельсин, манго и маракуйя." },

    // ---------- Комбо-завтраки ----------
    {
      id: "combo-1", cat: "combo", title: "Ланч №1", price: 450, badge: "Выгодно",
      meta: "Напиток + выпечка",
      desc: "Кофе или чай 300 мл и выпечка на выбор. Пончик можно добавить за доплату.",
      options: [
        {
          id: "drink", title: "Напиток, 300 мл", required: true,
          choices: [
            { id: "americano", title: "Американо", delta: 0 },
            { id: "cappuccino", title: "Капучино", delta: 0 },
            { id: "latte", title: "Латте", delta: 0 },
            { id: "raf", title: "Раф", delta: 40 },
            { id: "tea-black", title: "Чай чёрный", delta: 0 },
            { id: "tea-ginger", title: "Чай имбирный", delta: 0 },
            { id: "tea-seabuckthorn", title: "Чай облепиховый", delta: 0 },
          ],
        },
        {
          id: "main", title: "Выпечка", required: true,
          choices: [
            { id: "croissant-raspberry", title: "Круассан с малиной", delta: 0 },
            { id: "sandwich-ham-cheese", title: "Сэндвич с ветчиной и сыром", delta: 60 },
          ],
        },
        {
          id: "donut", title: "Пончик", required: true, default: "none",
          choices: [
            { id: "none", title: "Без пончика", delta: 0 },
            { id: "donut-strawberry", title: "Пончик с клубникой", delta: 120 },
            { id: "donut-chocolate", title: "Пончик шоколадный", delta: 120 },
            { id: "donut-caramel", title: "Пончик карамельный", delta: 120 },
          ],
        },
      ],
    },
    {
      id: "combo-2", cat: "combo", title: "Ланч №2", price: 720,
      meta: "Латте 300 мл + 2 позиции",
      desc: "Латте 300 мл, круассан с лососем и сосиска в тесте. Сытный завтрак на весь день.",
    },
    {
      id: "combo-kids", cat: "combo", title: "Детское комбо", price: 390,
      meta: "Сок + пончик + сырник",
      desc: "Детский сок 200 мл, пончик и сырник со сметаной.",
      options: [
        {
          id: "juice", title: "Сок", required: true, default: "juice-apple",
          choices: [
            { id: "juice-apple", title: "Яблочный", delta: 0 },
            { id: "juice-peach", title: "Персиковый", delta: 0 },
            { id: "juice-multifruit", title: "Мультифрукт", delta: 0 },
          ],
        },
        {
          id: "donut", title: "Пончик", required: true, default: "donut-strawberry",
          choices: [
            { id: "donut-strawberry", title: "С клубникой", delta: 0 },
            { id: "donut-chocolate", title: "Шоколадный", delta: 0 },
            { id: "donut-caramel", title: "Карамельный", delta: 0 },
          ],
        },
      ],
    },
  ],
};
