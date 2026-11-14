// Every X/Twitter DOM selector lives here. X changes its markup often;
// when the extension stops finding tweets, this is the only file to fix.
// Loaded as a classic content script: it defines a global, nothing else.
/* exported PPG_SEL */
var PPG_SEL = Object.freeze({
  // One rendered post (also used for the quoted post's container check).
  tweet: 'article[data-testid="tweet"]',
  // The post body. Its `lang` attribute is X's language guess.
  tweetText: '[data-testid="tweetText"]',
  // Display name + @handle block.
  userName: '[data-testid="User-Name"]',
  // Timestamp; its closest <a> is the permalink to /<handle>/status/<id>.
  time: 'time[datetime]',
  statusLink: 'a[href*="/status/"]',
  // "X reposted" line above a repost.
  socialContext: '[data-testid="socialContext"]',
  // Quoted post card inside a quote post. X renders it as a div[role=link].
  quoteCard: 'div[role="link"]',
  // Ads. NOT placementTracking: X also wraps every video player in it, so it
  // marked all video posts (and quotes of them) as ads. Ads are found by the
  // "Ad" label in the header (parse.js) or this explicit indicator.
  promotedMarkers: '[data-testid="promotedIndicator"]',
  // Action row (reply / repost / like / ...). Fallback badge anchor.
  actionRow: '[role="group"]',
});

// Status URL: /<handle>/status/<id>[/...]
var PPG_STATUS_RE = /^\/([A-Za-z0-9_]{1,15})\/status\/(\d{5,25})/;
// Text that marks a post as promoted when no testid is present.
var PPG_PROMOTED_TEXT = /^(Ad|Promoted|Mainos|Sponsoroitu|Gesponsert)$/;
// "reposted" in the languages we expect on the demo audience.
var PPG_REPOST_TEXT = /repost|retweet|uudelleenjulkais|reposteado|a reposté|teilte/i;
