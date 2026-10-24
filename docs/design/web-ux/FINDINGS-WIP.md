# Findings while capturing (kittrial-5bb.400, trial at main a89790a / 1024d)

## Project page, Tasks tab (#/p/pp), owner view, 1280 wide
- Height 2100 px; 302 words, 16 buttons, 8 inputs in main.
- Order, top to bottom:
  - breadcrumb and h1 with a "New task" button;
  - setup banner ("4 steps are left");
  - **Merge slot** (a form: Task id, Integration target, Take slot);
  - **Guidance and onboarding** (two h3 sections, two details);
  - **Lanes** (prose sentence, threshold input, a table, here an error: "Your session has ended. Sign in again" with a request id; release 1024e, .399, fixes the panel authority);
  - "Want the product to do something? Propose a requirement on Reviews";
  - status chips (Active/Closed/All) and review-state chips (8);
  - "Show requirements";
  - a stray "null";
  - **"Tasks in PP" table** (the page's main content) LAST;
  - another stray "null";
  - the pager.
- The task list starts about 1,400 px down: below two screens of coordinator tools on a 1280x800 display.
- Two literal "null" texts are rendered (a value with no guard).
- An error panel shows a request id to every reader (request ids are not for people).
- The "Next action" column repeats "Claim this task" on every unclaimed row.

## Task page (#/p/pp/t/pp-ghb), owner view, 1024 wide
- Height 5558 px (about seven screens); 641 words, 13 buttons, 29 inputs.
- Order, top to bottom:
  - breadcrumb;
  - h1 and "Edit";
  - badges: Open, No contribution, P2;
  - six lifecycle facts in upper case, all "Unknown";
  - **Lifecycle facts** h2: a paragraph, then four "—" key-values, then six "x: unknown" lines again, then Refresh, then two details (Integration check, Record lifecycle evidence);
  - "Next: Deliver a contribution — waiting on casey." (the most useful sentence, sixth block down);
  - **Assign task** form (roster button, select, reason, two checkboxes, button);
  - **Brief**, where the task's own description finally appears (about 1,000 px down), then "Where it stands", "Changes since this checkpoint", "Unresolved items";
  - **Contribution & review** ("No contribution yet");
  - the "Add a comment" form;
  - the "Record a checkpoint" form, always open (12 fields, including the lane state);
  - **History**;
  - task metadata (status, priority, assignee, created, updated, id) at the very bottom;
  - **Files**.
- Defects seen:
  - "Checkpoint 3 minutes ago by [object Object]": an object rendered as text.
  - The next-step sentence for a stale checkpoint is jargon: "STALE CHECKPOINT: activity the checkpoint did not incorporate exists (1 by other actors, 0 own; 0 outstanding direction(s) ... coverage TIMESTAMP — a backdated arrival ...)".
  - "Changes since this checkpoint" and History show raw machine records (`Kind: task-assignment-v1 {json}`, `Kind: task-checkpoint-v1 {json}` with activity cursors, account ids `usr_...` and operation ids) as if they were comments.
  - The six lifecycle facts are shown twice (badges, then lines).
  - The "Next action" is buried below lifecycle evidence the reader rarely needs.

## Records tab (#/p/pp/records), owner view
- Height 3312 px; 273 words, 4 buttons, 26 inputs.
- Order:
  - **Waiting to be accepted** ("3 waiting"): a paragraph, then a table of KIND/KEY/STAGE/TITLE/PROPOSED BY, each row with a "Review & accept" button;
  - **Find & filter records** (a 5-field form);
  - **Reference catalog** (the same three drafts again, plus columns);
  - the **"Propose a reference record"** form, always open (11 fields);
  - **Capability registry** ("0 shown");
  - the **"Propose a capability record"** form, always open (10 fields).
- **The Records confusion James named.** "Waiting to be accepted" is the first thing on the page and offers "Review & accept" to the owner on every row.
  - The paragraph says "until a coordinator accepts them", but nothing names WHO that is in this project (the coordinator agent), or that the owner need not act.
  - A person reading it as the owner sees a to-do list addressed to them.
  - The proposal must say who acts on each row: the project's coordinator (agent) accepts; the owner is told only when a decision is theirs.
- The same drafts are listed twice (the waiting table and the catalog).
- The authority column shows "—" for drafts whose authority is an attestation (the information is there; the cell is empty).
- Two long propose forms are always open, although proposing is a rare, mostly agent-made action.

## Room (#/p/pp/rooms/{rid}), owner view
- Short page (126 words); a header, then numbered messages (#n, author, role, time, text).
- Export links, the Post form, then a Moderation section ("Archive room", "Remove a message" by number).
- The thread is readable. To moderate, the owner types a message number rather than acting on the message itself.

## Rooms list
- Compact: one card per room. "Create a room" form below.

## My agents (#/agents), owner view
- One card per agent: name, tool, connection, "Could take more work", "Idle: 5 claimable task(s)", the folder, the resume prompt with Copy, scopes, "Set up folder", then five details (claim limit, folder, projects, may do, history).
- "Add an agent" form below. The card is dense, but folded well.
- Jargon: "claimable", "resume prompt (after setup)".

## My work (#/), owner view
- Order:
  - h1 and one sentence;
  - "Copy prompt for my agent";
  - My contributions;
  - "Your agents" (the same agent card as My agents, repeated);
  - Revisions requested;
  - Waiting for your review;
  - Assigned to you.
- **When nothing is waiting, three empty sections still take space.** The agent card is a second copy of My agents.

## Reviews (#/p/pp/reviews)
- A stack of h2 groups, each with a count: proposals, awaiting review, changes requested, approved not integrated.
- Every empty group still shows its prose and an empty state.

## Requirements (#/p/pp/requirements)
- Short; the explanations come before the (empty) document.

## The other pages (1024 wide, owner view)
- **Releases:** 172 words; a pre-flight form, then an 8-field "Record one release group" form, always open.
- **Members & settings** (3071 px, 555 words):
  - members table with a role select per row;
  - "How work is handed out";
  - "Who coordinates" (people, agents, operators, rules);
  - agents in the project;
  - worker credentials;
  - a 21-row audit table always open;
  - archive.
- **Set up** (4099 px, 1053 words): eight numbered steps. Each says Done/To do, who, and an explanation; two inline forms. This one reads well as a checklist, but done steps are as long as open ones.
- **How work is found** (11980 px, 6423 words): reference documentation served in the page. h3/h4 sections, then the prompts. It is the longest page, with no table of contents.
- **Projects:** compact; one card per project; the "New project" prose explains admin.py to people who cannot run it.
- **New task:** three fields; good.
- **Sign-in:** compact; good.

## Phone width (375 px)
- **Project page:** no sideways scroll; the task list starts about 1,640 px down (two full phone screens of coordinator tools first). The menu folds into a "Menu" button, which works.
- **Task page:** the page is 1,026 px wide at 375, so the whole page scrolls sideways.
  - The culprit is the Brief panel: the raw machine record shown under "Changes since this checkpoint" carries a 64-hex operation id and long JSON, and nothing wraps it.
  - The page is 6571 px tall. "Brief" (the description) starts at 1918 px, History at 5039 px, Files at 6301 px.
- **The h1 shows a focus ring**, because the app moves focus to the heading on navigation, which is right for screen readers. It looks like an editable box. A `:focus-visible`-only ring, or `outline-offset` and a subtler style, would fix it.
