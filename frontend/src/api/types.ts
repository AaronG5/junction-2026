/**
 * The contract between the Veritas Lens frontend and backend.
 *
 * Every /api endpoint returns exactly these shapes as JSON. The demo data
 * (src/demo) is written in the same types, so it doubles as the backend's
 * test fixtures. Dates are ISO strings: "YYYY-MM-DD" for days,
 * full ISO 8601 for moments.
 */

export type Platform = "Telegram" | "Facebook" | "Website";
export type Verdict = "true" | "false" | "misleading" | "unverified";

/** Number of items per status, after platform + date filters but before the status filter. */
export type StatusCounts = Record<"all" | Verdict, number>;

/* ------------------------------------------------------------------ filters */

export type PlatformFilter = "all" | "Telegram" | "Facebook";
export type RangePreset = "7" | "30" | "90" | "custom";
export type StatusFilter = "all" | Verdict;

/** Filter state in the UI. Sent to the API as platform, from, to, status. */
export interface Filters {
  platform: PlatformFilter;
  range: RangePreset;
  /** Only used when range is "custom". */
  from: string;
  to: string;
  status: StatusFilter;
}

/* -------------------------------------------------------------- narratives */

export interface Language {
  code: string;   // "ru", "lt", "en", ...
  name: string;   // "Russian"
  share: number;  // percent, 0..100
}

export interface PostRef {
  id: string;
  text: string;
  author: string;
  /** e.g. "Telegram channel · Lithuanian" */
  where: string;
}

export interface EdgePost extends PostRef {
  /** Why this post sits at the edge of its cluster. */
  note: string;
}

export interface Cluster {
  id: number;
  name: string;
  size: number;
  /** Change in size vs. the previous week, percent. null when the cluster is new. */
  changePct: number | null;
  accounts: number;
  /** Verdict of the cluster's main claim. */
  status: Verdict;
  platforms: Platform[];
  firstSeen: string;
  lastSeen: string;
  languages: Language[];
  /** Closest to the centre: read these to know what the cluster is about. */
  typical: PostRef[];
  /** Farthest from the centre but still inside. */
  edge: EdgePost[];
}

/** One post on the cluster map. x, y in 0..100. clusterId null = fits no cluster. */
export interface MapPoint {
  x: number;
  y: number;
  clusterId: number | null;
}

export interface NarrativesResponse {
  run: { date: string; posts: number; clusters: number; unclustered: number };
  /** Clusters matching the filters, largest first. */
  clusters: Cluster[];
  /** Total large clusters before filtering. */
  total: number;
  counts: StatusCounts;
  /** Points of the matching clusters plus unclustered posts. */
  points: MapPoint[];
}

/* ------------------------------------------------------------------- claims */

export type Stance = "supports" | "contradicts" | "context" | "neither";

export interface Evidence {
  source: string;
  stance: Stance;
  note: string;
  url?: string;
}

export interface Claim {
  id: number;
  text: string;
  verdict: Verdict;
  platforms: Platform[];
  firstSeen: string;
  lastSeen: string;
  posts: number;
  languages: string[];  // language codes, e.g. ["ru", "lt"]
  explanation: string;
  evidence: Evidence[];
  checkedBy: string;    // "web search + Gemma"
  confidence: number;   // 0..1
  checkedAt: string;
  clusterId: number | null;
}

export interface ClaimsResponse {
  claims: Claim[];
  total: number;
  counts: StatusCounts;
}

/* ------------------------------------------------------------------ sources */

export interface Source {
  id: string;
  name: string;
  /** e.g. "Channel · Lithuanian · 18.4k subscribers" */
  kind: string;
  platform: Platform;
  posts30d: number;
  originalPct: number;
  nearCopyPct: number;
  mainTopic: string;
  openFlags: number;
}

export interface SourcesResponse {
  sources: Source[];
}

export interface ChannelReport {
  id: string;
  name: string;
  platform: Platform;
  /** e.g. "Telegram channel" */
  typeLabel: string;
  /** e.g. "18.4k subscribers · writes in Lithuanian · created March 2023" */
  subtitle: string;
  /** e.g. "Based on 2,906 posts collected since March 2023." */
  basedOn: string;
  stats: { postsPerDay: number; originalPct: number; nearCopyPct: number; stateMediaLinkPct: number };
  /** Posts per day, oldest first, last 90 days. */
  daily: { date: string; posts: number }[];
  topics: { name: string; pct: number }[];
  upstream: { name: string; kind: string; posts: number; typicalDelay: string }[];
  recent: { id: string; text: string; publishedAt: string; views: number; verdict: Verdict }[];
}

/* ------------------------------------------------------------------ analyze */

/** A piece of post text. flag = the technique Gemma flagged this wording for. */
export interface TextSegment {
  text: string;
  flag?: string;
}

export interface ChainNode {
  name: string;
  kind: string;      // "Telegram channel", "News site · state media (RU)"
  language: string;  // "EN"
  time: string;      // "08:00"
  /** How this node got the content from the previous one. Empty for the first. */
  relation?: string;
  isThisPost?: boolean;
}

export interface PostReport {
  url: string;
  analyzedAt: string;
  answeredFrom: string;
  post: {
    author: string;
    sourceId: string | null;
    where: string;
    original: TextSegment[];
    originalLanguage: string; // "Lithuanian"
    translation: TextSegment[];
  };
  verdict: { verdict: Verdict; summary: string };
  /** 0..10 */
  manipulation: number;
  spread: { copies: number; languages: number; hours: number };
  earliest: { name: string; note: string };
  chain: ChainNode[];
  moreCopies: number;
  claims: { id: number; text: string; verdict: Verdict; explanation: string; evidenceSummary: string; seenSummary: string }[];
  similar: { language: string; author: string; where: string; text: string; relation: string; similarity: number }[];
  categories: { name: string; score: number; method: string }[];
  reading: { tone: string; intent: string; techniques: { name: string; detail: string }[]; model: string };
  cluster: { id: number; name: string; summary: string; languages: Language[] };
}

/** Progress steps shown while a link is analyzed, in order. */
export const ANALYZE_STEPS = [
  "Reading the post",
  "Making a text fingerprint (EmbeddingGemma)",
  "Finding similar posts in every language",
  "Checking the claims against sources",
  "Gemma reads the post",
] as const;

/* -------------------------------------------------------- landing / stories */

export interface Story {
  id: string;
  platform: Platform;
  status: Verdict;
  firstSeen: string;
  lastSeen: string;
  topic: string;
  trend: { posts: number; changePct: number | null };
  headline: string;
  sub: string;
  post: { author: string; where: string; text: string; translation: string };
  claim: { headline: string; text: string; verdict: Verdict; explanation: string; evidence: string };
  origin: { headline: string; chain: { meta: string; name: string; kind: string }[] };
  travel: { headline: string; copies: number; accounts: number; timeLabel: string; languages: { name: string; count: number }[] };
  narrative: { headline: string; name: string; stats: string };
}

export interface TrendingResponse {
  stories: Story[];
}

/* ------------------------------------------------------------------- system */

export interface SystemStatus {
  ai: {
    running: boolean;
    chatModel: string;
    chatModelNote: string;
    embeddingModel: string;
    embeddingNote: string;
    hardware: string;
    hardwareNote: string;
    /** A stronger model that fits this computer, if any. */
    upgrade: { model: string; downloadGb: number; note: string } | null;
  };
  services: { name: string; role: string; address: string; running: boolean }[];
}
