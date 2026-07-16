---
name: image-analysis
description: Reliably inspect images, screenshots, forms, and documents attached to a message.
always: true
---

# Image analysis safety

- Images attached to the current user message are already delivered as native image input.
- Inspect the attached pixels directly. Do not call `read_file` for the same path shown in an `[image: ...]` breadcrumb.
- For screenshots, forms, tables, and documents, transcribe the visible names, dates, labels, and values before summarizing or scheduling a reminder.
- State only details that are visibly supported. If the image is unavailable or unreadable, say that clearly and ask the user to resend it; never invent a visual description.
- When creating a reminder from an image, include the extracted visible details in the reminder message instead of referring only to “these people” or “this image”.
