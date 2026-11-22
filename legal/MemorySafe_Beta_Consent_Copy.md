# MemorySafe Consent Copy

**Version:** 0.4 - November 17, 2026

Use two separate controls. Do not combine acceptance of the legal documents with consent to Automatic Mode.

## Control 1 - MemorySafe services (optional)

This control is optional. Leaving it unticked must not block installing, running, or connecting MemorySafe: the software is licensed under the Apache License 2.0, and the Terms only cover the services MemorySafe Labs operates for it.

**Checkbox label**

Optional: I'd like to use MemorySafe's services, such as support. I agree to the MemorySafe Terms of Use and acknowledge the MemorySafe Privacy Policy.

**Supporting text**

Memory content is stored on this computer, in one database shared by the assistants you connect. Memory content reaches the assistant you are using, and whatever it includes in its own conversation goes to that assistant's provider under your agreement with them.

**Button**

Save and continue

## Control 2 - Optional Automatic Mode

**Heading**

Turn on Automatic Mode?

**Consent text**

When Automatic Mode is on, MemorySafe may save short, directly stated, durable, non-sensitive facts from chats where MemorySafe is selected. It keeps no copy of your conversations and does not read an assistant's own built-in memory. In Claude Code and Codex, it reads each message you send, on this computer, to save a preference you state plainly. Experimental filters attempt to avoid secrets, financial and government identifiers, contact details, precise addresses, and medical or health information. You can review saved memories and turn Automatic Mode off at any time. Turning it off stops future automatic captures but does not erase memories already stored.

**Primary button**

Turn on Automatic Mode

**Secondary button**

Not now

## Settings copy

**Automatic Mode on**

Automatic Mode is ON. MemorySafe may save eligible facts only in chats where MemorySafe is selected.

**Automatic Mode off**

Automatic Mode is OFF. New memories are saved only when you explicitly ask.

**Deletion note**

The Forget action removes a memory from active recall and can be undone. To delete a memory for good, run `memorysafe erase <memory id> --confirm`; to delete all local data, run `memorysafe erase --everything "erase everything"`. Contact contact@memorysafe.ca for help.

