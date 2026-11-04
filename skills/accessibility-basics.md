---
name: accessibility-basics
description: The WCAG basics every web page needs: alt text, labels, focus, contrast and keyboard reach
when: accessibility, accessible, a11y, wcag, screen reader, alt text, aria, keyboard navigation, color contrast, focus outline
files: *.html, *.jsx, *.tsx, *.vue, *.svelte, *.css, templates/*
---
# Accessibility basics

Most accessibility bugs are a missing attribute or a div pretending to be a button.

- **Images:** every `<img>` has `alt`. Describe what it conveys; use `alt=""` for purely decorative images.
- **Forms:** every input has a visible `<label for="id">`. A placeholder is not a label. Error messages name the field and are tied
  to it with `aria-describedby`.
- **Use real elements.** `<button>` for actions, `<a href>` for navigation. A clickable `<div>` cannot be reached by keyboard.
- **Keyboard reach:** every control works with Tab, Enter and Space, in a sensible order. Never use positive `tabindex`.
- **Visible focus:** do not remove `outline` unless you replace it with an equally visible style.
- **Contrast:** body text at least 4.5:1 against its background, large text and UI parts at least 3:1. Do not rely on colour alone
  to show state; add text or an icon.
- **Structure:** one `<h1>`, headings in order, `lang` on `<html>`, landmarks like `<main>` and `<nav>`.
- **ARIA last.** Prefer a native element; add `aria-*` only when no element does the job, and keep it accurate.

Check by unplugging the mouse and using the page with the keyboard only.
