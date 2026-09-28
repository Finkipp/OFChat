"""Small GTK desktop client for one-to-one XMPP chats."""

import sys
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from .storage import History
from .xmpp import Connection, bare_jid


APP_ID = "org.example.OFChat"


class ChatApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.history = None
        self.connection = Connection(self._from_network)
        self.account = None
        self.peer = None
        self.contacts = {}
        self.unread = {}
        self.contact_rows = {}
        self.connected = False
        self.window = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        action = Gio.SimpleAction.new("open-chat", GLib.VariantType.new("s"))
        action.connect("activate", self._open_notification)
        self.add_action(action)
        logout = Gio.SimpleAction.new("disconnect", None)
        logout.connect("activate", self._logout)
        self.add_action(logout)
        self.logout_action = logout
        logout.set_enabled(False)
        styles = Gtk.CssProvider()
        styles.load_from_data(b"""
            .presence-dot { font-size: 15px; }
            .presence-online { color: #2ec27e; }
            .presence-away { color: #e5a50a; }
            .presence-offline { color: #9a9996; }
            .unread-badge {
                background-color: #3584e4; color: white;
                border-radius: 12px; padding: 1px 7px;
                font-weight: bold; font-size: 11px;
            }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), styles, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.history = History()

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        self.window = Gtk.ApplicationWindow(application=self, title="OFChat")
        self.window.set_default_size(850, 580)
        self.window.set_icon_name("mail-message-new")
        self.window.connect("delete-event", self._on_close)
        self.window.connect("notify::is-active", self._window_active_changed)
        self.header = Gtk.HeaderBar(title="OFChat", show_close_button=True)
        menu = Gio.Menu()
        menu.append("Отключиться", "app.disconnect")
        menu_button = Gtk.MenuButton()
        menu_button.set_tooltip_text("Меню приложения")
        menu_button.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu_button.set_menu_model(menu)
        self.header.pack_end(menu_button)
        self.window.set_titlebar(self.header)
        self.stack = Gtk.Stack()
        self.window.add(self.stack)
        self._build_login()
        self._build_chat()
        self.stack.set_visible_child_name("login")
        self.window.show_all()

    def _build_login(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.set_border_width(32)
        outer.set_valign(Gtk.Align.CENTER)
        outer.set_halign(Gtk.Align.CENTER)
        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        title = Gtk.Label(label="Вход в Openfire")
        title.get_style_context().add_class("title")
        outer.pack_start(title, False, False, 0)
        for row, (label, placeholder, hidden) in enumerate([
            ("JID", "user@server.example", False),
            ("Пароль", "", True),
            ("Адрес сервера (необязательно)", "Например, 192.168.1.10", False),
            ("Порт", "5222", False),
        ]):
            entry = Gtk.Entry()
            entry.set_placeholder_text(placeholder)
            entry.set_visibility(not hidden)
            entry.set_width_chars(32)
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(entry, 1, row, 1, 1)
            if row == 0:
                self.jid_entry = entry
            elif row == 1:
                self.password_entry = entry
                entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
                entry.connect("activate", self._login)
            elif row == 2:
                self.host_entry = entry
            else:
                self.port_entry = entry
                entry.set_text("5222")
        outer.pack_start(grid, False, False, 0)
        self.login_button = Gtk.Button(label="Подключиться")
        self.login_button.connect("clicked", self._login)
        outer.pack_start(self.login_button, False, False, 0)
        self.login_status = Gtk.Label()
        self.login_status.set_line_wrap(True)
        outer.pack_start(self.login_status, False, False, 0)
        self.stack.add_named(outer, "login")

    def _build_chat(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_position(260)
        root.pack_start(split, True, True, 0)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_header = Gtk.Box(spacing=6)
        sidebar_header.set_margin_start(12)
        sidebar_header.set_margin_end(8)
        sidebar_header.set_margin_top(6)
        sidebar_header.set_margin_bottom(6)
        heading = Gtk.Label(label="Чаты", xalign=0)
        heading.get_style_context().add_class("heading")
        sidebar_header.pack_start(heading, True, True, 0)
        add = Gtk.Button()
        add.set_image(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON))
        add.set_tooltip_text("Открыть чат по JID")
        add.get_style_context().add_class("flat")
        add.connect("clicked", self._add_peer)
        sidebar_header.pack_end(add, False, False, 0)
        left.pack_start(sidebar_header, False, False, 0)
        scroll_contacts = Gtk.ScrolledWindow()
        scroll_contacts.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.contact_list = Gtk.ListBox()
        self.contact_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.contact_list.connect("row-selected", self._select_row)
        self.contact_list.get_style_context().add_class("navigation-sidebar")
        scroll_contacts.add(self.contact_list)
        left.pack_start(scroll_contacts, True, True, 0)
        split.pack1(left, resize=False, shrink=False)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_border_width(12)
        peer_header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.peer_label = Gtk.Label(label="Выберите контакт", xalign=0)
        self.peer_label.get_style_context().add_class("heading")
        peer_header.pack_start(self.peer_label, False, False, 0)
        self.peer_jid = Gtk.Label(xalign=0)
        self.peer_jid.get_style_context().add_class("dim-label")
        peer_header.pack_start(self.peer_jid, False, False, 0)
        right.pack_start(peer_header, False, False, 0)
        messages_scroll = Gtk.ScrolledWindow()
        messages_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.messages_scroll = messages_scroll
        self.messages = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.messages.set_left_margin(10)
        self.messages.set_right_margin(10)
        self.messages.set_top_margin(10)
        self.messages.set_bottom_margin(10)
        messages_scroll.add(self.messages)
        right.pack_start(messages_scroll, True, True, 0)
        compose = Gtk.Box(spacing=6)
        self.message_entry = Gtk.Entry(placeholder_text="Напишите сообщение…")
        self.message_entry.connect("activate", self._send)
        compose.pack_start(self.message_entry, True, True, 0)
        send = Gtk.Button()
        send.set_image(Gtk.Image.new_from_icon_name("mail-send-symbolic", Gtk.IconSize.BUTTON))
        send.set_tooltip_text("Отправить сообщение")
        send.connect("clicked", self._send)
        compose.pack_start(send, False, False, 0)
        attention = Gtk.Button()
        attention.set_image(Gtk.Image.new_from_icon_name("emblem-important-symbolic", Gtk.IconSize.BUTTON))
        attention.set_tooltip_text("Отправить XMPP Attention (XEP-0224)")
        attention.connect("clicked", self._send_attention)
        compose.pack_start(attention, False, False, 0)
        right.pack_start(compose, False, False, 0)
        split.pack2(right, resize=True, shrink=False)
        self.stack.add_named(root, "chat")

    def _login(self, *_args):
        try:
            jid = bare_jid(self.jid_entry.get_text())
            port = int(self.port_entry.get_text())
            if not 1 <= port <= 65535:
                raise ValueError("Порт должен быть от 1 до 65535")
            if not self.password_entry.get_text():
                raise ValueError("Введите пароль")
            self.connection.start(jid, self.password_entry.get_text(),
                                  self.host_entry.get_text().strip() or None, port)
        except (ValueError, RuntimeError) as exc:
            self.login_status.set_text(str(exc))
            return
        self.account = jid
        self.password_entry.set_text("")
        self.login_status.set_text("Подключение…")
        self.login_button.set_sensitive(False)

    def _from_network(self, event, data):
        GLib.idle_add(self._handle_event, event, data)

    def _handle_event(self, event, data):
        if event == "connected":
            self.connected = True
            self.account = data
            self.header.set_subtitle(data)
            self.logout_action.set_enabled(True)
            self.stack.set_visible_child_name("chat")
            self.peer = None
            self.unread.clear()
            self.contacts = {peer: (peer, "offline") for peer in self.history.peers(data)}
            self._refresh_contacts()
        elif event == "roster":
            self.contacts.update(data)
            if self.connected:
                self._refresh_contacts()
        elif event == "message":
            peer, body = data
            if not self.account:
                return False
            self.history.add(self.account, peer, "in", body)
            self._ensure_peer(peer)
            if self.peer == peer:
                self._append("in", body)
            if not self.window.is_active() or self.peer != peer:
                self._increment_unread(peer)
            if not self.window.is_active():
                self._notify(peer, body)
        elif event == "attention":
            peer = data
            self._ensure_peer(peer)
            if self.peer == peer:
                self._append("system", "Просит обратить внимание")
            if not self.window.is_active() or self.peer != peer:
                self._increment_unread(peer)
            if not self.window.is_active():
                self.window.set_urgency_hint(True)
                self._notify(peer, "Просит обратить внимание", urgent=True)
        elif event == "certificate":
            self._confirm_certificate(data)
        elif event == "error":
            if self.connected:
                self.header.set_subtitle(data)
            else:
                self.login_status.set_text(data)
                self.login_button.set_sensitive(True)
        elif event == "disconnected":
            self.connected = False
            self.logout_action.set_enabled(False)
            self.header.set_subtitle(None)
            self.login_button.set_sensitive(True)
            if self.stack.get_visible_child_name() == "chat" or self.login_status.get_text() == "Подключение…":
                self.login_status.set_text("Соединение закрыто" + (f": {data}" if data else ""))
            self.stack.set_visible_child_name("login")
        return False

    def _confirm_certificate(self, data):
        dialog = Gtk.Dialog(title="Сертификат сервера Openfire", transient_for=self.window, modal=True)
        dialog.add_button("Отклонить", Gtk.ResponseType.CANCEL)
        dialog.add_button("Доверять сертификату", Gtk.ResponseType.OK)
        dialog.set_default_size(600, -1)
        details = (
            f"Сервер: {data['host']}:{data['port']} (домен XMPP: {data['domain']})\n\n"
            f"Причина: {data['reason'] or 'Ранее одобренный сертификат изменился'}\n\n"
            f"SHA-256: {data['fingerprint']}"
        )
        if data["previous"]:
            details += f"\n\nПредыдущий SHA-256: {data['previous']}"
        label = Gtk.Label(label=details, xalign=0)
        label.set_selectable(True)
        label.set_line_wrap(True)
        label.set_max_width_chars(70)
        label.set_margin_top(16)
        label.set_margin_bottom(16)
        label.set_margin_start(16)
        label.set_margin_end(16)
        dialog.get_content_area().add(label)
        dialog.show_all()
        approved = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        self.connection.approve_certificate(approved)
        if approved:
            self.login_status.set_text("Подключение с одобренным сертификатом…")

    def _notify(self, peer, text, urgent=False):
        notification = Gio.Notification.new(self.contacts.get(peer, (peer,))[0])
        notification.set_body(text)
        notification.set_default_action_and_target("app.open-chat", GLib.Variant("s", peer))
        if urgent:
            notification.set_priority(Gio.NotificationPriority.URGENT)
        self.send_notification(peer, notification)

    def _open_notification(self, action, parameter):
        peer = parameter.get_string()
        self.activate()
        self._ensure_peer(peer)
        self._open_peer(peer)

    def _refresh_contacts(self):
        selected = self.peer
        self.contact_rows = {}
        self._updating_contacts = True
        for row in self.contact_list.get_children():
            self.contact_list.remove(row)
        for jid, (name, status) in sorted(self.contacts.items(), key=lambda item: item[1][0].lower()):
            row = Gtk.ListBoxRow()
            row.jid = jid
            content = Gtk.Box(spacing=10)
            content.set_margin_start(12)
            content.set_margin_end(12)
            content.set_margin_top(8)
            content.set_margin_bottom(8)
            dot = Gtk.Label(label="●")
            dot.set_tooltip_text({"online": "Активен", "away": "Отошёл", "offline": "Не в сети"}[status])
            dot.get_style_context().add_class("presence-dot")
            dot.get_style_context().add_class("presence-" + status)
            content.pack_start(dot, False, False, 0)
            label = Gtk.Label(label=name, xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_tooltip_text(jid)
            content.pack_start(label, True, True, 0)
            badge = Gtk.Label()
            badge.get_style_context().add_class("unread-badge")
            content.pack_end(badge, False, False, 0)
            row.badge = badge
            row.add(content)
            self.contact_rows[jid] = row
            self.contact_list.add(row)
            if jid == selected:
                self.contact_list.select_row(row)
        self.contact_list.show_all()
        for jid in self.contact_rows:
            self._update_unread_badge(jid)
        self._updating_contacts = False

    def _update_unread_badge(self, peer):
        row = self.contact_rows.get(peer)
        if row:
            count = self.unread.get(peer, 0)
            row.badge.set_text("99+" if count > 99 else str(count))
            row.badge.set_visible(count > 0)

    def _increment_unread(self, peer):
        self.unread[peer] = self.unread.get(peer, 0) + 1
        self._update_unread_badge(peer)

    def _mark_read(self, peer):
        if peer and self.unread.pop(peer, None):
            self._update_unread_badge(peer)
        if peer:
            self.withdraw_notification(peer)

    def _window_active_changed(self, window, property_spec):
        if window.is_active():
            self._mark_read(self.peer)
            window.set_urgency_hint(False)

    def _ensure_peer(self, peer):
        if peer not in self.contacts:
            self.contacts[peer] = (peer, "offline")
            self._refresh_contacts()

    def _select_row(self, listbox, row):
        if row and not self._updating_contacts and row.jid != self.peer:
            self._open_peer(row.jid)

    def _open_peer(self, peer):
        self.peer = peer
        self.peer_label.set_text(self.contacts.get(peer, (peer,))[0])
        self.peer_jid.set_text(peer)
        row = self.contact_rows.get(peer)
        if row and self.contact_list.get_selected_row() != row:
            self.contact_list.select_row(row)
        buffer = self.messages.get_buffer()
        buffer.set_text("")
        if self.account:
            for direction, body, timestamp in self.history.recent(self.account, peer):
                self._append(direction, body, timestamp)
        self._mark_read(peer)
        self.window.set_urgency_hint(False)
        self.message_entry.grab_focus()

    def _append(self, direction, body, timestamp=None):
        timestamp = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        author = {"in": self.contacts.get(self.peer, (self.peer,))[0],
                  "out": "Вы", "system": "Внимание"}[direction]
        buffer = self.messages.get_buffer()
        buffer.insert(buffer.get_end_iter(), f"[{timestamp[:16]}] {author}: {body}\n")
        GLib.idle_add(lambda: self.messages_scroll.get_vadjustment().set_value(
            self.messages_scroll.get_vadjustment().get_upper()))

    def _send(self, *_args):
        body = self.message_entry.get_text().strip()
        if not body or not self.peer:
            return
        try:
            if not self.connected:
                raise RuntimeError("Нет подключения к серверу")
            self.connection.send_message(self.peer, body)
        except RuntimeError as exc:
            self.header.set_subtitle(str(exc))
            return
        self.history.add(self.account, self.peer, "out", body)
        self._append("out", body)
        self.message_entry.set_text("")

    def _send_attention(self, *_args):
        if not self.peer:
            return
        try:
            if not self.connected:
                raise RuntimeError("Нет подключения к серверу")
            self.connection.send_attention(self.peer)
            self._append("system", "Вы попросили обратить внимание")
        except RuntimeError as exc:
            self.header.set_subtitle(str(exc))

    def _add_peer(self, *_args):
        dialog = Gtk.Dialog(title="Открыть чат", transient_for=self.window, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Открыть", Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text="user@server.example")
        entry.set_margin_top(12)
        entry.set_margin_bottom(12)
        entry.set_margin_start(12)
        entry.set_margin_end(12)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            try:
                peer = bare_jid(entry.get_text())
                self._ensure_peer(peer)
                self._open_peer(peer)
            except ValueError as exc:
                self.header.set_subtitle(str(exc))
        dialog.destroy()

    def _logout(self, *_args):
        self.connected = False
        self.logout_action.set_enabled(False)
        self.header.set_subtitle(None)
        self.connection.stop()
        self.login_button.set_sensitive(True)
        self.stack.set_visible_child_name("login")

    def _on_close(self, *_args):
        self.connection.stop()
        self.history.close()
        return False


def main():
    return ChatApplication().run(sys.argv)
