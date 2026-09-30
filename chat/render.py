"""GTK rendering of the supported Markdown subset and copyable code blocks."""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk

from .formatting import blocks, inline_markup


def render_message(body):
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    for kind, language, text in blocks(body):
        if kind == "text":
            if not text:
                continue
            label = Gtk.Label(xalign=0)
            label.set_selectable(True)
            label.set_line_wrap(True)
            label.set_markup(inline_markup(text))
            content.pack_start(label, False, False, 0)
            continue
        code_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        code_box.get_style_context().add_class("code-block")
        toolbar = Gtk.Box(spacing=8)
        name = Gtk.Label(label=language or "Код", xalign=0)
        name.get_style_context().add_class("dim-label")
        toolbar.pack_start(name, True, True, 0)
        copy = Gtk.Button(label="Копировать")
        copy.get_style_context().add_class("flat")
        copy.connect("clicked", lambda button, code=text: Gtk.Clipboard.get(
            Gdk.SELECTION_CLIPBOARD).set_text(code, -1))
        toolbar.pack_end(copy, False, False, 0)
        code_box.pack_start(toolbar, False, False, 0)
        view = _code_view(text, language)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroll.set_min_content_height(min(340, max(50, (text.count("\n") + 1) * 21 + 16)))
        scroll.add(view)
        code_box.pack_start(scroll, False, False, 0)
        content.pack_start(code_box, False, False, 0)
    return content


def _code_view(text, language):
    try:
        gi.require_version("GtkSource", "4")
        from gi.repository import GtkSource
        buffer = GtkSource.Buffer()
        language_name = {"py": "python", "js": "js", "sh": "sh"}.get(language, language)
        syntax = GtkSource.LanguageManager.get_default().get_language(language_name)
        if syntax:
            buffer.set_language(syntax)
            buffer.set_highlight_syntax(True)
        view = GtkSource.View.new_with_buffer(buffer)
    except (ImportError, ValueError):
        view = Gtk.TextView()
        buffer = view.get_buffer()
    buffer.set_text(text)
    view.set_editable(False)
    view.set_cursor_visible(False)
    view.set_wrap_mode(Gtk.WrapMode.NONE)
    view.set_left_margin(8)
    view.set_top_margin(5)
    return view
