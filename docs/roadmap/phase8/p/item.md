---
title: "Reply after reading: let an approved reply go to the sender of a message the run read (design debate first)"
status: idea
links: [phase8/c, phase6/m]
---
**Problem (found 2026-11-10 by the local multistep eval):** in 2 of 3 `thread_reply_by_mail` runs the model found the email and drafted the right reply, but `mail_send` was refused outright: reading the email marked the run untrusted, and egress after untrusted content is denied, not sent for approval (phase8/c). So 'reply to her' can't work in one request, the most common thing an assistant does with mail. The eval task expects an approved send, so the task and the policy disagree.

**Options to debate:** (a) keep denying, and make the refusal hand off cleanly (a fresh untainted run drafts from trusted context only); (b) allow egress after taint only through approval, only to a recipient already in the thread or a known contact, with the approval card showing the whole body and flagging links and any text copied from the untrusted message; (c) a reply draft the user sends themselves (pmjc_handoff). Risk to weigh: the body itself can leak data that injected text asked for.
