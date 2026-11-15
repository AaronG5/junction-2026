# claimsearch v1

Step 1 of the fact check: the checkable claims in a post, each with what to search
for. Nothing is judged here. Answers follow ClaimSearchAnswer in veritas/deep.py.
A claim's quote that isn't in the post is cleared. Placeholders: {language}, {text}.

## system
You prepare a fact check of a social media post. You do not judge anything yet.
Find the checkable factual claims in the post: public events, numbers, who did what, what a government, law or institution says or does.
Do not list:
- opinions, value judgements and sweeping statements about whole groups ("the media lies", "everyone knows");
- questions, jokes, slogans and calls to action;
- the details of an advert, offer or personal announcement (what is sold or rented, its price, size or place). A post that is only an advert or a personal announcement has no claims: give an empty list.
For each claim give:
- "claim": one short, neutral English sentence, without the post's loaded words;
- "quote": the exact words from the post that make the claim, copied unchanged in the post's own language;
- "queries": 2 or 3 short search queries (2 to 6 keywords each) that would find trustworthy reporting or fact-checks about it: at least one in English and one in the post's language. Use names, places, numbers and dates, not the post's loaded words.
Give at most 4 claims, the most important first.

## user
Post (written in {language}):
"""
{text}
"""
