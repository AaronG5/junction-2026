/**
 * DEMO DATA: invented examples, loaded only in demo mode (npm run dev:demo).
 * Never shown when the app talks to the real backend.
 *
 * Dates are relative to today so the demo stays inside "last 7 days".
 * Written in the API contract types, so it also serves as backend test fixtures.
 */
import type {
  ChannelReport, Claim, Cluster, Language, NarrativeCheckStatus, NarrativeResult, PostReport, Source, Story, SystemStatus,
} from "../api/types";
import { shiftDays, todayIso } from "../lib/filters";

/** Marker used by the build check to prove demo data is absent from normal builds. */
export const DEMO_DATA_MARKER = "veritas-lens-demo-data-v1";

const today = todayIso();
/** A day relative to today: d(-3) = three days ago. */
export const d = (offset: number) => shiftDays(today, offset);

const L = (ru: number, lt: number, other: number, otherName = "Other"): Language[] => [
  { code: "ru", name: "Russian", share: ru },
  { code: "lt", name: "Lithuanian", share: lt },
  { code: "xx", name: otherName, share: other },
];

/* ---------------------------------------------------------------- narratives */

export interface DemoCluster extends Cluster {
  /** Map placement for generated points (demo only). */
  map: { x: number; y: number; sx: number; sy: number; n: number };
}

export const clusters: DemoCluster[] = [
  {
    id: 12, name: "NATO “provocations” at the Belarus border", size: 1284, changePct: 38, accounts: 212,
    status: "false", platforms: ["Telegram", "Facebook"], firstSeen: d(-12), lastSeen: d(0), languages: L(47, 41, 12, "English"),
    typical: [
      { id: "p-4521", url: "https://t.me/kanalas_b/4521", text: "NATO tankai vėl provokuoja prie Baltarusijos sienos…", author: "@kanalas_b", where: "Telegram channel · Lithuanian" },
      { id: "p-a-881", text: "Танки НАТО снова провоцируют у белорусской границы", author: "@kanal_a", where: "Telegram channel · Russian" },
    ],
    edge: [{ id: "p-fb-77", text: "Pratybos prie sienos: ar tikrai reikia jas rengti dabar?", author: "Tiesos balsas", where: "Facebook page · Lithuanian", note: "Asks a question instead of claiming aggression. Sits next to the media-distrust cluster." }],
    map: { x: 24, y: 38, sx: 7, sy: 6, n: 34 },
  },
  {
    id: 7, name: "EU sanctions will double electricity prices", size: 962, changePct: 12, accounts: 148,
    status: "false", platforms: ["Telegram", "Facebook"], firstSeen: d(-17), lastSeen: d(-1), languages: L(52, 38, 10),
    typical: [
      { id: "p-e-1", url: "https://t.me/kanalas_b/4498", text: "Elektros kainos kils 40% dėl ES sankcijų…", author: "@kanalas_b", where: "Telegram channel · Lithuanian" },
      { id: "p-e-2", text: "Из-за санкций ЕС свет в Литве подорожает вдвое", author: "@novosti_baltii", where: "Telegram channel · Russian" },
    ],
    edge: [{ id: "p-e-3", text: "Šildymo sezonas brangsta: kaip sutaupyti?", author: "Taupi šeima", where: "Facebook group · Lithuanian", note: "Practical advice. Same topic, no claim about sanctions." }],
    map: { x: 63, y: 26, sx: 6, sy: 5, n: 26 },
  },
  {
    id: 3, name: "Election counting is rigged", size: 741, changePct: -5, accounts: 97,
    status: "unverified", platforms: ["Telegram"], firstSeen: d(-14), lastSeen: d(-4), languages: L(30, 64, 6),
    typical: [
      { id: "p-v-1", text: "Rinkimų komisija slepia balsų skaičiavimo klaidas", author: "@kanalas_b", where: "Telegram channel · Lithuanian" },
      { id: "p-v-2", text: "ЦИК Литвы скрывает ошибки подсчёта", author: "@kanal_a", where: "Telegram channel · Russian" },
    ],
    edge: [{ id: "p-v-3", text: "Kaip veikia balsų skaičiavimas: paaiškinimas", author: "Pilietinė iniciatyva", where: "Facebook page · Lithuanian", note: "An explainer that uses the same words. Probably not part of the narrative." }],
    map: { x: 72, y: 70, sx: 6, sy: 6, n: 21 },
  },
  {
    id: 15, name: "Refugees get bigger benefits than pensioners", size: 533, changePct: 21, accounts: 71,
    status: "misleading", platforms: ["Facebook", "Telegram"], firstSeen: d(-20), lastSeen: d(0), languages: L(44, 52, 4),
    typical: [
      { id: "p-r-1", text: "Pabėgėliai gauna didesnes išmokas nei pensininkai", author: "Tiesa be cenzūros", where: "Facebook page · Lithuanian" },
      { id: "p-r-2", text: "Беженцы получают больше пенсионеров", author: "@novosti_baltii", where: "Telegram channel · Russian" },
    ],
    edge: [{ id: "p-r-3", text: "Savivaldybė skelbia paramos pabėgėliams tvarką", author: "Municipality page", where: "Facebook page · Lithuanian", note: "Official notice about the same benefits. Gets quoted out of context." }],
    map: { x: 36, y: 76, sx: 6, sy: 5, n: 16 },
  },
  {
    id: 9, name: "Western media hide the truth", size: 410, changePct: 4, accounts: 88,
    status: "misleading", platforms: ["Telegram"], firstSeen: d(-24), lastSeen: d(-2), languages: L(58, 33, 9),
    typical: [
      { id: "p-m-1", text: "Kodėl apie tai tyli mūsų žiniasklaida?", author: "@kanalas_b", where: "Telegram channel · Lithuanian" },
      { id: "p-m-2", text: "Западные СМИ скрывают правду о провокациях", author: "@kanal_a", where: "Telegram channel · Russian" },
    ],
    edge: [{ id: "p-m-3", text: "Visuomeninis transliuotojas atsakė į kritiką dėl reportažo", author: "News site", where: "Website · Lithuanian", note: "A media outlet answering criticism. Same vocabulary, opposite side." }],
    map: { x: 47, y: 50, sx: 5, sy: 4, n: 13 },
  },
  {
    id: 21, name: "Baltic power grid will collapse this winter", size: 288, changePct: null, accounts: 42,
    status: "unverified", platforms: ["Telegram"], firstSeen: d(-4), lastSeen: d(0), languages: L(61, 35, 4),
    typical: [
      { id: "p-g-1", text: "Šią žiemą Baltijos elektros tinklas sugrius", author: "@kanalas_b", where: "Telegram channel · Lithuanian" },
      { id: "p-g-2", text: "Энергосистема Прибалтики рухнет зимой", author: "@kanal_a", where: "Telegram channel · Russian" },
    ],
    edge: [{ id: "p-g-3", text: "Sinchronizacija su Europa: ką tai reiškia namų ūkiams", author: "News site", where: "Website · Lithuanian", note: "Neutral explainer on the same topic." }],
    map: { x: 86, y: 40, sx: 4, sy: 5, n: 9 },
  },
];

export const run = { date: d(0), posts: 18420, clusters: 14, unclustered: 1904 };

/* -------------------------------------------------------------------- claims */

export const claims: Claim[] = [
  { id: 1, verdict: "false", platforms: ["Telegram", "Facebook"], firstSeen: d(-11), lastSeen: d(-1), posts: 23, languages: ["ru", "lt", "en"], clusterId: 12,
    text: "NATO tanks are carrying out provocations at the Belarusian border.",
    explanation: "The tanks are part of an exercise announced a week earlier, held about 40 km from the border. Border reports for that day list no incidents.",
    evidence: [
      { source: "Border guard daily report", stance: "contradicts", note: "No incidents recorded on the Belarus section." },
      { source: "Armed forces exercise announcement", stance: "contradicts", note: "Lists the exercise area and dates; the area is inland." },
    ],
    checkedBy: "web search + Gemma", confidence: 0.86, checkedAt: d(-10) },
  { id: 2, verdict: "misleading", platforms: ["Telegram"], firstSeen: d(-11), lastSeen: d(-5), posts: 11, languages: ["lt", "ru"], clusterId: 9,
    text: "Lithuanian media are not reporting it.",
    explanation: "National outlets covered the exercise. They did not report a provocation because none was recorded.",
    evidence: [{ source: "3 national news articles", stance: "contradicts", note: "Each describes the exercise as planned and routine." }],
    checkedBy: "web search + Gemma", confidence: 0.78, checkedAt: d(-10) },
  { id: 3, verdict: "false", platforms: ["Telegram", "Facebook"], firstSeen: d(-15), lastSeen: d(-3), posts: 38, languages: ["ru", "lt"], clusterId: 7,
    text: "Electricity prices will double because of EU sanctions.",
    explanation: "The regulator's published forecast for next quarter is a change of a few percent. Sanctions do not set household tariffs.",
    evidence: [{ source: "Energy regulator tariff forecast", stance: "contradicts", note: "Forecast change is in single digits." }],
    checkedBy: "web search + Gemma", confidence: 0.82, checkedAt: d(-14) },
  { id: 4, verdict: "unverified", platforms: ["Telegram"], firstSeen: d(-13), lastSeen: d(-7), posts: 17, languages: ["lt", "ru"], clusterId: 3,
    text: "The election commission is hiding vote-counting errors.",
    explanation: "No evidence either way yet. Recount results are due next week.",
    evidence: [{ source: "Election commission recount notice", stance: "neither", note: "Confirms a recount in two districts, says nothing about hidden errors." }],
    checkedBy: "web search + Gemma", confidence: 0.41, checkedAt: d(-12) },
  { id: 5, verdict: "misleading", platforms: ["Facebook", "Telegram"], firstSeen: d(-20), lastSeen: d(-2), posts: 29, languages: ["lt", "ru"], clusterId: 15,
    text: "Refugees receive higher benefits than pensioners.",
    explanation: "Compares a one-time settlement grant with a monthly pension. Monthly support for refugees is lower than the average pension.",
    evidence: [
      { source: "Municipal support rules for refugees", stance: "contradicts", note: "Monthly amount is below the average pension." },
      { source: "Social insurance pension statistics", stance: "context", note: "Average pension for comparison." },
    ],
    checkedBy: "web search + Gemma", confidence: 0.74, checkedAt: d(-19) },
  { id: 6, verdict: "true", platforms: ["Facebook"], firstSeen: d(-13), lastSeen: d(-9), posts: 9, languages: ["lt"], clusterId: null,
    text: "A protest against new taxes will take place in Vilnius this week.",
    explanation: "The city lists a permitted gathering at that time and place.",
    evidence: [{ source: "City events and permits list", stance: "supports", note: "Gathering approved for that evening." }],
    checkedBy: "web search + Gemma", confidence: 0.9, checkedAt: d(-12) },
  { id: 7, verdict: "false", platforms: ["Telegram"], firstSeen: d(-62), lastSeen: d(-44), posts: 44, languages: ["ru", "lt"], clusterId: null,
    text: "Lithuania will start conscripting women next year.",
    explanation: "No such law has been proposed or passed. The claim misreads a voluntary training programme.",
    evidence: [{ source: "Parliament legislation register", stance: "contradicts", note: "No bill on conscripting women." }],
    checkedBy: "web search + Gemma", confidence: 0.88, checkedAt: d(-61) },
];

/* ------------------------------------------------------------------- sources */

export const sources: Source[] = [
  { id: "kanal_a", name: "@kanal_a", kind: "Channel · Russian", platform: "Telegram", posts30d: 1940, originalPct: 61, nearCopyPct: 12, mainTopic: "Anti-NATO", openFlags: 3 },
  { id: "kanalas_b", name: "@kanalas_b", kind: "Channel · Lithuanian · 18.4k subscribers", platform: "Telegram", posts30d: 690, originalPct: 8, nearCopyPct: 64, mainTopic: "Anti-NATO", openFlags: 5 },
  { id: "novosti_baltii", name: "@novosti_baltii", kind: "Channel · Russian", platform: "Telegram", posts30d: 1120, originalPct: 22, nearCopyPct: 41, mainTopic: "Energy prices", openFlags: 2 },
  { id: "baltijos_balsas", name: "Baltijos balsas", kind: "Group · Lithuanian · 4.2k members", platform: "Telegram", posts30d: 3410, originalPct: 35, nearCopyPct: 48, mainTopic: "Elections", openFlags: 1 },
  { id: "prawda_wschod", name: "@prawda_wschod", kind: "Channel · Polish", platform: "Telegram", posts30d: 520, originalPct: 18, nearCopyPct: 52, mainTopic: "Anti-NATO", openFlags: 0 },
  { id: "tiesa_be_cenzuros", name: "Tiesa be cenzūros", kind: "Page · Lithuanian · 9.1k followers", platform: "Facebook", posts30d: 410, originalPct: 3, nearCopyPct: 77, mainTopic: "Refugees", openFlags: 7 },
  { id: "taupi_seima", name: "Taupi šeima", kind: "Group · Lithuanian · 12k members", platform: "Facebook", posts30d: 260, originalPct: 81, nearCopyPct: 6, mainTopic: "Everyday prices", openFlags: 0 },
  { id: "rt_com", name: "rt.com", kind: "News site · state media (RU)", platform: "Website", posts30d: 2200, originalPct: 94, nearCopyPct: 2, mainTopic: "Anti-NATO", openFlags: 0 },
];

/* ---------------------------------------------------------- channel report */

const last30 = [14, 17, 19, 15, 22, 18, 16, 21, 24, 19, 17, 23, 20, 18, 22, 26, 19, 58, 41, 29, 24, 21, 25, 22, 19, 23, 27, 21, 24, 26];
const first60 = Array.from({ length: 60 }, (_, i) => Math.round(12 + 6 * Math.abs(Math.sin(i * 0.7)) + (i % 7 === 3 ? 6 : 0)));

export const channelReports: Record<string, ChannelReport> = {
  kanalas_b: {
    id: "kanalas_b", name: "@kanalas_b", platform: "Telegram", typeLabel: "Telegram channel",
    subtitle: "18.4k subscribers · writes in Lithuanian · created March 2023",
    basedOn: "Based on 2,906 posts collected since March 2023.",
    stats: { postsPerDay: 23, originalPct: 8, nearCopyPct: 64, stateMediaLinkPct: 31 },
    daily: [...first60, ...last30].map((posts, i, all) => ({ date: d(i - (all.length - 1)), posts })),
    topics: [
      { name: "Anti-NATO", pct: 38 }, { name: "Energy prices", pct: 21 }, { name: "Elections", pct: 14 },
      { name: "Media distrust", pct: 12 }, { name: "Other", pct: 15 },
    ],
    upstream: [
      { name: "@kanal_a", kind: "Telegram channel · Russian", posts: 212, typicalDelay: "1 h 40 min" },
      { name: "rt.com", kind: "News site · state media (RU)", posts: 88, typicalDelay: "3 h 10 min" },
      { name: "@prawda_wschod", kind: "Telegram channel · Polish", posts: 41, typicalDelay: "5 h" },
      { name: "@novosti_baltii", kind: "Telegram channel · Russian", posts: 36, typicalDelay: "2 h 20 min" },
    ],
    recent: [
      { id: "p-4521", url: "https://t.me/kanalas_b/4521", text: "NATO tankai vėl provokuoja prie Baltarusijos sienos…", publishedAt: d(-11) + "T11:00:00Z", views: 1912, verdict: "misleading" },
      { id: "p-e-1", url: "https://t.me/kanalas_b/4498", text: "Elektros kainos kils 40% dėl ES sankcijų…", publishedAt: d(-12) + "T18:20:00Z", views: 2340, verdict: "false" },
      { id: "p-t-1", url: "https://t.me/kanalas_b/4490", text: "Vilniuje vyks protestas prieš naujus mokesčius", publishedAt: d(-12) + "T09:05:00Z", views: 1104, verdict: "true" },
      { id: "p-v-1", url: "https://t.me/kanalas_b/4476", text: "Rinkimų komisija slepia balsų skaičiavimo klaidas", publishedAt: d(-13) + "T21:40:00Z", views: 3018, verdict: "unverified" },
    ],
  },
};

/* --------------------------------------------------------------- post report */

export const postReport: PostReport = {
  url: "https://t.me/kanalas_b/4521",
  analyzedAt: new Date().toISOString(),
  answeredFrom: "the central server (2,184 related posts) and this computer",
  post: {
    author: "@kanalas_b", sourceId: "kanalas_b",
    where: "Telegram channel · 18.4k subscribers",
    originalLanguage: "Lithuanian",
    original: [
      { text: "„NATO tankai vėl " }, { text: "provokuoja", flag: "Loaded language" },
      { text: " prie Baltarusijos sienos. " }, { text: "Kodėl apie tai tyli mūsų žiniasklaida?", flag: "Media distrust" }, { text: "“" },
    ],
    translation: [
      { text: "“NATO tanks are " }, { text: "provoking", flag: "Loaded language" },
      { text: " at the Belarusian border again. " }, { text: "Why is our media silent about it?", flag: "Media distrust" }, { text: "”" },
    ],
  },
  verdict: { verdict: "misleading", summary: "1 false claim, 1 misleading" },
  manipulation: 7,
  spread: { copies: 41, languages: 3, hours: 6 },
  earliest: { name: "rt.com", note: "3 h before this post · state media (RU)" },
  chain: [
    { name: "rt.com", kind: "News site · state media (RU)", language: "EN", time: "08:00" },
    { name: "@kanal_a", kind: "Telegram channel", language: "RU", time: "09:30", relation: "translation" },
    { name: "@kanalas_b", kind: "This post", language: "LT", time: "11:00", relation: "forward", isThisPost: true },
    { name: "Tiesa be cenzūros", kind: "Facebook page", language: "LT", time: "12:40", relation: "share" },
  ],
  moreCopies: 37,
  claims: [
    { id: 1, text: "NATO tanks are carrying out provocations at the Belarusian border.", verdict: "false",
      explanation: "The tanks are part of a scheduled exercise about 40 km from the border, announced a week earlier. Border reports for that day list no incidents.",
      evidenceSummary: "2 sources contradict it", seenSummary: "Seen in 23 posts, 3 languages" },
    { id: 2, text: "Lithuanian media are not reporting it.", verdict: "misleading",
      explanation: "National outlets covered the exercise; they did not report a provocation because none was recorded.",
      evidenceSummary: "3 articles found", seenSummary: "Seen in 11 posts, 2 languages" },
  ],
  similar: [
    { language: "EN", author: "rt.com", where: "News site", text: "NATO armour stages provocations near Belarus, sources say", relation: "Earliest source", similarity: 0.79 },
    { language: "RU", author: "@kanal_a", where: "Telegram channel", text: "Танки НАТО снова провоцируют у белорусской границы", relation: "Translation", similarity: 0.81 },
    { language: "LT", author: "Tiesa be cenzūros", where: "Facebook page", text: "NATO tankai vėl provokuoja prie Baltarusijos sienos…", relation: "Share", similarity: 0.97 },
    { language: "LT", author: "Jonas P.", where: "in Baltijos balsas · Telegram group", text: "Matot? NATO tankai vėl provokuoja prie sienos", relation: "Near copy", similarity: 0.93 },
    { language: "PL", author: "@prawda_wschod", where: "Telegram channel", text: "Czołgi NATO znowu prowokują przy granicy z Białorusią", relation: "Paraphrase", similarity: 0.77 },
  ],
  categories: [
    { name: "Anti-NATO", score: 0.9, method: "Matched by similarity, confirmed by Gemma" },
    { name: "Media distrust", score: 0.62, method: "Matched by similarity only" },
  ],
  reading: {
    tone: "Alarmist",
    intent: "Make readers distrust NATO and local media",
    techniques: [
      { name: "Loaded language", detail: "“provokuoja” frames a routine exercise as aggression." },
      { name: "Media distrust", detail: "A rhetorical question suggests a cover-up without evidence." },
      { name: "No source", detail: "No place, date or source is given for the claim." },
    ],
    model: "gemma-4-E4B-it · prompt v3 · ran on this computer",
  },
  cluster: { id: 12, name: "NATO “provocations” at the Belarus border", summary: "1,284 posts in the last 7 days · up 38% on the week before", languages: L(47, 41, 12, "English") },
};

/* --------------------------------------------------------- landing stories */

export const stories: Story[] = [
  {
    id: "nato-border", platform: "Telegram", status: "false", firstSeen: d(-12), lastSeen: d(0),
    topic: "Anti-NATO", trend: { posts: 1284, changePct: 38 },
    headline: "“NATO tanks are provoking at the Belarus border again”", sub: "Telegram · Lithuanian",
    post: { url: "https://t.me/kanalas_b/4521", author: "@kanalas_b", where: "Telegram channel", text: "„NATO tankai vėl provokuoja prie Baltarusijos sienos. Kodėl apie tai tyli mūsų žiniasklaida?“", translation: "“NATO tanks are provoking at the Belarusian border again. Why is our media silent about it?”" },
    claim: { headline: "Its main claim doesn't hold up.", text: "NATO tanks are carrying out provocations at the border", verdict: "false",
      explanation: "The tanks are part of an exercise announced a week earlier, held about 40 km from the border. Border reports for that day list no incidents.",
      evidence: "The border guard's daily report and the armed forces' exercise announcement both contradict it." },
    origin: { headline: "It started on Russian state media, 3 hours earlier.", chain: [
      { meta: "08:00 · English", name: "rt.com", kind: "News site · Russian state media" },
      { meta: "09:30 · Russian · translated", name: "@kanal_a", kind: "Telegram channel" },
      { meta: "11:00 · Lithuanian · forwarded", name: "@kanalas_b", kind: "The post you picked" },
      { meta: "12:40 · Lithuanian · shared", name: "Tiesa be cenzūros", kind: "Facebook page" },
    ] },
    travel: { headline: "Within 6 hours it had been copied 41 times, in three languages.", copies: 41, accounts: 29, timeLabel: "6 h",
      languages: [{ name: "Russian", count: 19 }, { name: "Lithuanian", count: 17 }, { name: "English", count: 5 }] },
    narrative: { headline: "Yes. It's one of 1,284 posts telling the same story this week.", name: "NATO “provocations” at the Belarus border", stats: "1,284 posts from 212 accounts this week · up 38% on the week before" },
  },
  {
    id: "electricity", platform: "Telegram", status: "false", firstSeen: d(-17), lastSeen: d(-1),
    topic: "Energy prices", trend: { posts: 962, changePct: 12 },
    headline: "“EU sanctions will double electricity prices in Lithuania”", sub: "Telegram · Russian",
    post: { url: "https://t.me/novosti_baltii/20871", author: "@novosti_baltii", where: "Telegram channel", text: "„Из-за санкций ЕС свет в Литве подорожает вдвое уже этой зимой.“", translation: "“Because of EU sanctions, electricity in Lithuania will cost twice as much this winter.”" },
    claim: { headline: "Its main claim doesn't hold up.", text: "Electricity prices will double because of EU sanctions", verdict: "false",
      explanation: "The energy regulator forecasts a change of a few percent for next quarter. Sanctions don't set household tariffs.",
      evidence: "The energy regulator's published tariff forecast contradicts it." },
    origin: { headline: "It started in a Russian-language channel, then crossed into Lithuanian.", chain: [
      { meta: "07:10 · Russian", name: "@energy_insider_ru", kind: "Telegram channel" },
      { meta: "08:30 · Russian · near copy", name: "@novosti_baltii", kind: "The post you picked" },
      { meta: "18:20 · Lithuanian · translated", name: "@kanalas_b", kind: "Telegram channel" },
      { meta: "next day 09:05 · Lithuanian · shared", name: "Taupi šeima", kind: "Facebook group · 12k members" },
    ] },
    travel: { headline: "Over two days it was copied 38 times, mostly in Russian.", copies: 38, accounts: 24, timeLabel: "2 days",
      languages: [{ name: "Russian", count: 23 }, { name: "Lithuanian", count: 15 }] },
    narrative: { headline: "Yes. It's part of a storyline about energy and sanctions.", name: "EU sanctions will double electricity prices", stats: "962 posts from 148 accounts this week · up 12% on the week before" },
  },
  {
    id: "refugee-benefits", platform: "Facebook", status: "misleading", firstSeen: d(-20), lastSeen: d(0),
    topic: "Refugees", trend: { posts: 533, changePct: 21 },
    headline: "“Refugees get bigger benefits than pensioners”", sub: "Facebook · Lithuanian",
    post: { url: "https://www.facebook.com/tiesabecenzuros/posts/1188", author: "Tiesa be cenzūros", where: "Facebook page", text: "„Pabėgėliai gauna didesnes išmokas nei mūsų pensininkai. Kas čia vyksta?“", translation: "“Refugees get bigger benefits than our pensioners. What is going on?”" },
    claim: { headline: "It's not false, but it's misleading.", text: "Refugees receive higher benefits than pensioners", verdict: "misleading",
      explanation: "It compares a one-time settling-in grant with a monthly pension. Monthly support for refugees is lower than the average pension.",
      evidence: "Municipal support rules and pension statistics: the monthly amounts don't support the comparison." },
    origin: { headline: "It started in Russian, four and a half hours earlier.", chain: [
      { meta: "10:00 · Russian", name: "@novosti_baltii", kind: "Telegram channel" },
      { meta: "14:30 · Lithuanian · translated", name: "Tiesa be cenzūros", kind: "The post you picked" },
      { meta: "16:10 · Lithuanian · near copy", name: "Baltijos balsas", kind: "Telegram group · 4.2k members" },
    ] },
    travel: { headline: "In three days it was copied 29 times, mostly in Lithuanian.", copies: 29, accounts: 21, timeLabel: "3 days",
      languages: [{ name: "Lithuanian", count: 18 }, { name: "Russian", count: 11 }] },
    narrative: { headline: "Yes. It feeds a growing storyline about refugees and welfare.", name: "Refugees get bigger benefits than pensioners", stats: "533 posts from 71 accounts this week · up 21% on the week before" },
  },
];

/* -------------------------------------------------------------------- system */

export const systemStatus: SystemStatus = {
  ai: {
    running: true,
    chatModel: "gemma-4-E2B-it", chatModelNote: "Runs fully on the graphics card · reads images",
    embeddingModel: "embeddinggemma-300m", embeddingNote: "768 numbers per post · 100+ languages",
    hardware: "RTX 4070 Laptop · 8 GB", hardwareNote: "32 GB memory · 313 GB free disk",
    upgrade: { model: "gemma-4-26B-A4B-it", downloadGb: 15.4, note: "It gives better analyses but answers a little slower." },
  },
  services: [
    { name: "AI server", role: "llama-swap · OpenAI-compatible API", address: "127.0.0.1:8080", running: true },
    { name: "Database", role: "MySQL 8.4 · posts, sources, claims", address: "127.0.0.1:3307", running: true },
    { name: "Vector search", role: "Qdrant · similar-post search", address: "127.0.0.1:6335", running: true },
  ],
};

/* ---------------------------------------------------------- narrative check */

export { CLEAN_EXAMPLE, FLAGGED_EXAMPLE } from "../components/NarrativeCheck";

/** What the backend answers for a post that asserts a known narrative and copies a Telegram post (invented). */
export const narrativeFlagged: NarrativeResult = {
  id: "demo-flagged",
  status: "done",
  content: {
    label: "potentially_propaganda",
    score: 1,
    narrative: { text: "Ukraine is a puppet state run by the United States and NATO.", source: "euvsdisinfo", sim: 0.87 },
    techniques: [
      { code: "NC", name: "name-calling", quote: "The Kyiv regime" },
      { code: "LL", name: "loaded language", quote: "an American puppet" },
      { code: "CO", name: "causal oversimplification", quote: "ordinary Ukrainians die for NATO's war" },
      { code: "SL", name: "slogan", quote: "Wake up!" },
    ],
    reason: "The post states as fact that Washington controls Ukraine's government.",
    state_media_echo: [
      { text: "Kyiv takes its orders from Washington.", source: "rt", n_aired: 253, first: "2024-01-07", last: "2024-10-02", sim: 0.82 },
    ],
  },
  source_match: {
    strong: true,
    telegram: [
      { channel: "kanal_a", url: "https://t.me/kanal_a/58214", date: `${d(-2)}T18:49:44Z`, sim: 0.91,
        text: "Киевский режим — всего лишь американская марионетка. Вашингтон приказывает, Зеленский подчиняется…" },
      { channel: "novosti_baltii", url: "https://t.me/novosti_baltii/9120", date: `${d(-5)}T07:12:00Z`, sim: 0.74,
        text: "Украинцы гибнут за войну НАТО, а Киев выполняет приказы США." },
    ],
  },
  coordination: { label: "none", reasons: [], n_similar_accounts: 0 },
};

/** A post about everyday life: nothing found (invented). */
export const narrativeClean: NarrativeResult = {
  id: "demo-clean",
  status: "done",
  content: {
    label: "none",
    score: 0,
    narrative: { text: "Western food sanctions have emptied Baltic shops.", source: "euvsdisinfo", sim: 0.43 },
    techniques: [],
    reason: "",
    state_media_echo: [],
  },
  source_match: {
    strong: false,
    telegram: [{ channel: "kanalas_b", url: "https://t.me/kanalas_b/4410", date: `${d(-9)}T10:00:00Z`, sim: 0.38, text: "Turgaus kainos vėl kyla…" }],
  },
  coordination: { label: "none", reasons: [], n_similar_accounts: 0 },
};

/** GET /api/ext/status in demo mode: the real bank's sizes and thresholds. */
export const narrativeStatus: NarrativeCheckStatus = {
  available: true,
  reason: null,
  claims: 18601,
  echoClaims: 86023,
  telegramPosts: 42675,
  thresholds: { copy_strong: 0.815, candidate_gate: 0.6, echo_min: 0.65 },
  judge: true,
  embeddingModel: "embeddinggemma-300M (768-d, llama.cpp)",
  dim: 768,
};
