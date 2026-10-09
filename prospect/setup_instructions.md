<!--
REVIEW BEFORE SENDING (Process Pro internal, delete this block when done):
The Service Key menu path and scope names below were written from the hand-off and HubSpot's
changelog, NOT checked against a live portal's scope picker (docs.hubspot.com was unreachable
when this was drafted). Create a key in a portal we control, confirm every name in scopes.md and
the click path in step 2, then remove this comment.
-->

# HubSpot Audit: Read-Only Access Setup

## What

Process Pro will run a HubSpot audit of your portal. To do that without logging in to your account, we need you to create a **read-only Service Key** and send it to us, then complete a short checklist of items HubSpot's API cannot share.

Why it matters: the audit is faster (no scheduled screen-share), and it works from totals and percentages. Process Pro does not receive your contact names, emails or phone numbers.

What the key can and cannot do: it can only **read**. It cannot create, change or delete anything in your portal. You can delete the key at any time and access stops immediately.

## How

Time needed: about 15 minutes for the key, 20 to 30 minutes for the checklist. You need Super Admin access.

1. **Open Service Keys.** In HubSpot, go to Settings, then Integrations, then Service Keys.
2. **Create the key.** Click Create service key. Name it `Process Pro Audit (read-only)`.
3. **Add scopes.** Add only the read scopes listed in `scopes.md`. Do not add any scope that ends in `.write`. If a scope in the list is not offered in your account, skip it and tell us which one.
4. **Copy the key** once it is created.
5. **Send the key to Process Pro securely.** Use the one-time-secret link or password manager share your Process Pro contact gives you. Do not send it in an ordinary email or chat message.
6. **Fill in the intake form and `screenshot_checklist.md`** and send them to your Process Pro contact.
7. **When we confirm the audit is finished, delete the key.** Settings, then Integrations, then Service Keys, then the key, then Delete. We will remind you.

## Done

You are done when all four are true:

- [ ] The key exists, is named as above, and has only read scopes (compare against `scopes.md`).
- [ ] Your Process Pro contact has confirmed they received the key through the secure channel.
- [ ] The intake form and screenshot checklist are sent back.
- [ ] After Process Pro confirms the audit has run, the key is deleted.

Process Pro will tell you if any scope is missing; adding one takes a minute and does not change the key.
