---
name: thought-leadership
description: Uses the company's thought leadership database (via the thought-leadership MCP server) to answer "what's our stance on X", find content gaps, spot trending themes, turn transcripts into posts, build case studies and write content briefs grounded in what the company's own people have said. Use whenever the user asks about the company's opinions, expertise, content ideas, content calendar, briefs, or wants to save something a colleague said or wrote.
---

# Thought leadership

The `thought-leadership` MCP server holds what the company's people have said and written
(Slack, meetings, voice notes, interviews, documents, posts), each tagged by theme and
narrative, with an author, a citation and a visibility.

## Always

1. Call `get_taxonomy` first so you use the company's real theme and narrative names.
2. Pick the playbook that fits the request and call `get_playbook` with its name:

   | The user wants... | Playbook |
   | --- | --- |
   | Our position on a topic, or material for a piece | `query` |
   | What to make next, gaps, a content calendar | `coverage` |
   | To refresh existing content | `updates` |
   | What people are talking about | `trends` |
   | Posts from a long transcript or document | `atomise` |
   | The story of a topic over time | `aggregate` |
   | A case study from client work | `case-studies` |
   | Stories in our own data | `mining` |
   | Provocative content from internal disagreement | `debate` |
   | Predictions we got right | `foresight` |
   | Behind-the-scenes learning stories | `post-mortems` |
   | A content brief | `briefs` |
   | Podcast, video or infographic ideas | `media` |

3. Follow it.

## Rules that apply to every playbook

- Cite every item you use with its `citation`.
- Items with a `private_flag` are internal. Use them to shape ideas, but don't quote them,
  name the meeting or channel, or treat them as publishable without the author's approval.
  Tell the user which parts of your answer rest on private items.
- Read items in full (`get_item`) before relying on them. Summaries lose the best lines.
- Items marked `paraphrased` are meeting notes, a summary by a notetaker or an AI. Report what
  they say ("according to the meeting notes"), but never quote them as someone's own words.
- Chat is saved as sessions, split into topics. A topic's `views` summarise each person's
  position: use them to find what matters, then quote the messages they point to, never the
  view's own wording. When a topic is part of a chain, `get_chain` returns the whole thread.
- Keep the speaker's voice and strong opinions.
- Only save (`add_item`) or change (`update_item`) items when the user asks. Only make an
  item public when the user confirms the author approved it.
- When content gets made from an item, offer to record it with `add_deliverable`.
