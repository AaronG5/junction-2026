// Facebook: finds the posts in the data Facebook downloads (GraphQL answers and the JSON
// built into the page) and turns each into an API item (API.md), plus:
//   platform: "facebook", group: the group's name or null.
// Pure data, no DOM. Safe to run in Node.
//
// A feed post is an object with __typename "Story" and a post_id. Where its parts are
// (the same on the home feed, groups and pages):
//   url          permalink_url
//   time         creation_time (Unix seconds); also comet_sections.timestamp.story.creation_time
//   text         comet_sections.content.story.message.text
//   author       actors[0]: name, and url (profile link) for the handle
//   group        the "to" object with __typename "Group"
// Ads carry sponsored_data and are skipped. A shared post's original (attached_story)
// belongs to the sharing post and is not searched.
/* exported PPG_fbParseJson, PPG_fbExtract, PPG_fbTextKey */

// GraphQL answers are one or more JSON documents, one per line, sometimes behind "for (;;);".
function PPG_fbParseJson(text) {
  var docs = [];
  if (typeof text !== 'string' || !text) return docs;
  var lines = text.split('\n');
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim();
    if (line.indexOf('for (;;);') === 0) line = line.slice(9);
    if (line.charAt(0) !== '{' && line.charAt(0) !== '[') continue;
    try { docs.push(JSON.parse(line)); } catch (e) { /* not JSON, or cut off */ }
  }
  return docs;
}

// Letters and digits only, lower case: how a post's text on screen is matched to its data.
// (Facebook draws emoji as images and cuts long posts at "See more", so the rest differs.)
function PPG_fbTextKey(text) {
  return String(text || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
}

var PPG_fbExtract = (function () {
  function get(o, path) {
    for (var i = 0; i < path.length; i++) { if (o == null) return undefined; o = o[path[i]]; }
    return o;
  }

  function isFeedStory(o) {
    return o.__typename === 'Story' && o.post_id && (o.permalink_url || o.url) && Array.isArray(o.actors);
  }

  // Search inside one story, not entering the shared original (attached_story).
  function findInStory(story, test) {
    var stack = [story], steps = 0;
    while (stack.length && steps++ < 50000) {
      var o = stack.pop();
      if (!o || typeof o !== 'object') continue;
      if (o !== story && test(o)) return o;
      for (var k in o) {
        if (k !== 'attached_story' && o[k] && typeof o[k] === 'object') stack.push(o[k]);
      }
    }
    return null;
  }

  // "https://www.facebook.com/vilma.mamaityte" -> "vilma.mamaityte"; profile.php?id=N -> N
  function handleOf(actor) {
    try {
      var u = new URL(actor.url);
      return u.searchParams.get('id') || u.pathname.replace(/^\/+|\/+$/g, '') || actor.id || null;
    } catch (e) { return actor && actor.id ? String(actor.id) : null; }
  }

  function toItem(story) {
    var t = story.creation_time || get(story, ['comet_sections', 'timestamp', 'story', 'creation_time']);
    var text = get(story, ['comet_sections', 'content', 'story', 'message', 'text']) || get(story, ['message', 'text']) || '';
    var group = (story.to && story.to.__typename === 'Group' && story.to) ||
      findInStory(story, function (o) { return o.__typename === 'Group' && typeof o.name === 'string' && o.name; });
    var actor = story.actors[0] || {};
    var item = {
      id: 'fb' + String(story.post_id).replace(/[^A-Za-z0-9]/g, '').slice(0, 38),
      url: story.permalink_url || story.url,
      author: handleOf(actor),
      author_name: actor.name || '',
      text: String(text).trim(),
      lang: null,
      time: typeof t === 'number' ? new Date(t * 1000).toISOString() : null,
      kind: 'post',
      quoted_id: null,
      platform: 'facebook',
      group: group ? group.name : null,
    };
    var shared = sharedOf(story.attached_story);
    if (shared) item.shared = shared; // for the source trace (src/deep.js); not sent with the fast check
    return item;
  }

  // The original of a shared post (attached_story), when Facebook sends it along.
  function sharedOf(s) {
    if (!s || typeof s !== 'object') return null;
    var actor = (Array.isArray(s.actors) && s.actors[0]) || {};
    var url = s.permalink_url || s.url || null;
    var text = get(s, ['comet_sections', 'message', 'story', 'message', 'text']) || get(s, ['message', 'text']) || '';
    var t = s.creation_time || get(s, ['comet_sections', 'timestamp', 'story', 'creation_time']);
    if (!url && !text) return null;
    return {
      author: actor.url ? handleOf(actor) : (actor.id ? String(actor.id) : null),
      author_name: actor.name || null,
      url: typeof url === 'string' ? url : null,
      time: typeof t === 'number' ? new Date(t * 1000).toISOString() : null,
      text: String(text).trim().slice(0, 2000),
      platform: 'facebook',
    };
  }

  // All feed posts in one JSON document. Ads and posts without text are left out.
  return function PPG_fbExtract(doc) {
    var items = [], stack = [doc];
    while (stack.length) {
      var o = stack.pop();
      if (!o || typeof o !== 'object') continue;
      if (isFeedStory(o)) {
        if (!o.sponsored_data && !findInStory(o, function (x) { return !!x.sponsored_data; })) {
          var item = toItem(o);
          if (item.text) items.push(item);
        }
        continue; // its insides (including a shared original) belong to this post
      }
      for (var k in o) if (o[k] && typeof o[k] === 'object') stack.push(o[k]);
    }
    return items;
  };
})();
