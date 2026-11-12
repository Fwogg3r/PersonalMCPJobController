---
title: "Discord: DM people by name (guild member lookup), save them as contacts, aliases from chat"
status: done
links: [phase8/r]
---
**Owner request (2026-11-12):** "Message Emme" should find Emme in the bot's servers instead of asking for user ids; save contacts after the first time; aliases ("Anti is Maceo irl").

**Built:** `discord_members` (read, untrusted output: names are third-party text) lists or searches the members of every server the bot is in (full list with the Server Members intent, else Discord's name search; clear error when the intent is off), one row per person, no bots, best name matches only. `discord_dm`/`discord_dm_read` take `contact=<name>`: a saved contact with a `discord_user_id`, else the one member matching the contact's Discord names, name and aliases (or the name given); several matches are listed instead of guessed. After an approved DM the person is saved as a contact (`connectors.discord.save_contacts`, default on; owner-tier because the owner approved the DM). New setting `contacts_from_chat: pending|direct` (default pending): direct saves agent contacts and aliases at once in clean runs, but changing an existing handle still waits for approval. Friend lists: Discord gives bots no access, so not possible.

**Owner decisions (2026-11-12):** save after an approved DM; aliases saved directly (`contacts_from_chat: direct` in the owner's settings).
