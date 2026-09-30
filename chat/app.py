"""Small GTK desktop client for one-to-one XMPP chats."""

import mimetypes
import re
import sys
import uuid
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from .attachments import FileStore
from .accounts import Preferences, clear_password, load_password, store_password
from .render import render_message
from .storage import History
from .version import __version__
from .xmpp import MAX_FILE, Connection, bare_jid


APP_ID = "org.example.Quark"


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
        self.visible_limit = 100
        self.message_rows = {}
        self.peer_states = {}
        self._own_state = None
        self._typing_timer = None
        self.connected = False
        self.window = None
        self.tray = None
        self._held_in_tray = False
        self._indicator_status = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        action = Gio.SimpleAction.new("open-chat", GLib.VariantType.new("s"))
        action.connect("activate", self._open_notification)
        self.add_action(action)
        for name, callback in (
            ("disconnect", self._logout), ("broadcast", self._broadcast),
            ("export-chat", self._export_chat),
            ("about", self._about), ("quit-quark", self._quit_app),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if name in ("disconnect", "broadcast", "export-chat"):
                action.set_enabled(False)
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
            .message-out { background-color: alpha(@theme_selected_bg_color, 0.22); border-radius: 12px; padding: 10px; }
            .message-in { background-color: alpha(@theme_fg_color, 0.07); border-radius: 12px; padding: 10px; }
            .code-block { background-color: alpha(@theme_fg_color, 0.08); border-radius: 6px; padding: 6px; }
            .message-failed { color: #e01b24; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), styles, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.history = History()
        self.files = FileStore()
        self.preferences = Preferences()
        self._expire_files()
        GLib.timeout_add_seconds(60, self._expire_files)

    def _expire_files(self):
        if self.history:
            self.history.expire_attachments(self.files.directory)
        return True

    def do_activate(self):
        if self.window:
            self._show_window()
            return
        self.window = Gtk.ApplicationWindow(application=self, title="Quark")
        self.window.set_default_size(850, 580)
        self.window.set_icon_name("org.example.Quark")
        self.icon_path = Path(__file__).resolve().parent.parent / "icons"
        if self.icon_path.is_dir():
            Gtk.IconTheme.get_default().append_search_path(str(self.icon_path))
        self.window.connect("delete-event", self._on_close)
        self.window.connect("notify::is-active", self._window_active_changed)
        self.header = Gtk.HeaderBar(title="Quark", show_close_button=True)
        menu = Gio.Menu()
        files = Gio.Menu()
        files.append("Экспорт переписки…", "app.export-chat")
        actions = Gio.Menu()
        actions.append("Рассылка…", "app.broadcast")
        menu.append_submenu("Файл", files)
        menu.append_submenu("Действия", actions)
        menu.append("О программе", "app.about")
        menu.append("Отключиться", "app.disconnect")
        menu.append("Выйти из Quark", "app.quit-quark")
        menu_button = Gtk.MenuButton()
        menu_button.set_tooltip_text("Меню приложения")
        menu_button.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu_button.set_menu_model(menu)
        self.header.pack_end(menu_button)
        search_button = Gtk.Button()
        search_button.set_image(Gtk.Image.new_from_icon_name("edit-find-symbolic", Gtk.IconSize.BUTTON))
        search_button.set_tooltip_text("Поиск по переписке")
        search_button.get_style_context().add_class("flat")
        search_button.connect("clicked", self._toggle_search)
        self.header.pack_end(search_button)
        self.window.set_titlebar(self.header)
        self.stack = Gtk.Stack()
        self.window.add(self.stack)
        self._build_login()
        self._build_chat()
        self.stack.set_visible_child_name("login")
        self.window.show_all()
        self._build_tray()
        saved = self.preferences.load()
        if saved.get("jid"):
            self.jid_entry.set_text(saved["jid"])
            self.host_entry.set_text(saved.get("host", ""))
            self.port_entry.set_text(str(saved.get("port", 5222)))
            try:
                password = load_password(saved["jid"])
            except (RuntimeError, GLib.Error):
                password = None
            if password:
                self.password_entry.set_text(password)
                self.remember_check.set_active(True)
                self.autostart_check.set_active(bool(saved.get("autostart")))
                if saved.get("autostart"):
                    GLib.idle_add(self._auto_connect)

    def _auto_connect(self):
        if not self.connected and self.password_entry.get_text():
            self._login()
        return False

    def _build_tray(self):
        menu = Gtk.Menu()
        self.tray_open = Gtk.MenuItem(label="Показать Quark")
        self.tray_open.connect("activate", self._show_window)
        menu.append(self.tray_open)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Выйти")
        quit_item.connect("activate", self._quit_app)
        menu.append(quit_item)
        menu.show_all()
        self.tray_menu = menu
        try:
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3
            indicator = AppIndicator3.Indicator.new(
                APP_ID, "org.example.Quark", AppIndicator3.IndicatorCategory.COMMUNICATIONS
            )
            if self.icon_path.is_dir():
                indicator.set_icon_theme_path(str(self.icon_path))
            indicator.set_menu(menu)
            indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self._indicator_status = AppIndicator3.IndicatorStatus
            self.tray = indicator
        except (ImportError, ValueError):
            # Traditional notification areas still support Gtk.StatusIcon.
            icon = Gtk.StatusIcon.new_from_icon_name("org.example.Quark")
            icon.set_tooltip_text("Quark")
            icon.connect("activate", self._toggle_window)
            icon.connect("popup-menu", lambda widget, button, time: menu.popup(
                None, None, Gtk.StatusIcon.position_menu, widget, button, time
            ))
            self.tray = icon

    def _show_window(self, *_args):
        self.window.present()
        self._send_chatstate("active")
        if self._held_in_tray:
            self.release()
            self._held_in_tray = False

    def _toggle_window(self, *_args):
        if self.window.get_visible():
            self._on_close()
        else:
            self._show_window()

    def _update_tray_count(self):
        total = sum(self.unread.values())
        if self.tray and hasattr(self.tray, "set_label"):
            self.tray.set_label(str(total) if total else "", "99+")
        elif self.tray:
            self.tray.set_tooltip_text(f"Quark — непрочитанных: {total}" if total else "Quark")

    def _set_chat_actions(self, enabled):
        for name in ("disconnect", "broadcast", "export-chat"):
            self.lookup_action(name).set_enabled(enabled)

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
        self.remember_check = Gtk.CheckButton(label="Сохранить вводимые данные (пароль — в системной ключнице)")
        self.remember_check.connect("toggled", lambda checkbox: self.autostart_check.set_active(False)
                                    if not checkbox.get_active() else None)
        outer.pack_start(self.remember_check, False, False, 0)
        self.autostart_check = Gtk.CheckButton(label="Входить автоматически при запуске компьютера")
        self.autostart_check.connect("toggled", lambda checkbox: self.remember_check.set_active(True)
                                     if checkbox.get_active() else None)
        outer.pack_start(self.autostart_check, False, False, 0)
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
        self.typing_label = Gtk.Label(xalign=0)
        self.typing_label.get_style_context().add_class("dim-label")
        peer_header.pack_start(self.typing_label, False, False, 0)
        right.pack_start(peer_header, False, False, 0)
        self.search_bar = Gtk.SearchBar()
        self.search_entry = Gtk.SearchEntry(placeholder_text="Найти в переписке…")
        self.search_entry.connect("search-changed", self._search_changed)
        self.search_bar.add(self.search_entry)
        right.pack_start(self.search_bar, False, False, 0)
        messages_scroll = Gtk.ScrolledWindow()
        messages_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.messages_scroll = messages_scroll
        self.message_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.message_column.set_border_width(12)
        messages_scroll.add(self.message_column)
        right.pack_start(messages_scroll, True, True, 0)
        compose = Gtk.Box(spacing=6)
        composer_scroll = Gtk.ScrolledWindow()
        composer_scroll.set_min_content_height(52)
        composer_scroll.set_max_content_height(130)
        self.message_entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.message_entry.set_left_margin(8)
        self.message_entry.set_top_margin(8)
        self.message_entry.connect("key-press-event", self._composer_key)
        self.message_entry.connect("populate-popup", self._composer_menu)
        self.message_entry.get_buffer().connect("changed", self._composer_changed)
        composer_scroll.add(self.message_entry)
        compose.pack_start(composer_scroll, True, True, 0)
        attach = Gtk.Button()
        attach.set_image(Gtk.Image.new_from_icon_name("mail-attachment-symbolic", Gtk.IconSize.BUTTON))
        attach.set_tooltip_text("Прикрепить файл (до 5 МБ)")
        attach.connect("clicked", self._attach_file)
        compose.pack_start(attach, False, False, 0)
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
        self.search_bar.set_search_mode(False)

    def _toggle_search(self, *_args):
        if not self.connected or not self.peer:
            return
        self.search_bar.set_search_mode(not self.search_bar.get_search_mode())
        if self.search_bar.get_search_mode():
            self.search_entry.grab_focus()
        else:
            self.search_entry.set_text("")

    def _search_changed(self, *_args):
        self.visible_limit = 100
        self._render_conversation(scroll_to_bottom=False)

    def _login(self, *_args):
        try:
            jid = bare_jid(self.jid_entry.get_text())
            port = int(self.port_entry.get_text())
            if not 1 <= port <= 65535:
                raise ValueError("Порт должен быть от 1 до 65535")
            password = self.password_entry.get_text()
            if not password:
                raise ValueError("Введите пароль")
            host = self.host_entry.get_text().strip()
            if self.remember_check.get_active():
                store_password(jid, password)
                self.preferences.save(jid, host, port, self.autostart_check.get_active())
                self.preferences.set_autostart(self.autostart_check.get_active())
            else:
                old_jid = self.preferences.load().get("jid")
                if old_jid:
                    try:
                        clear_password(old_jid)
                    except (RuntimeError, GLib.Error):
                        pass
                self.preferences.path.unlink(missing_ok=True)
                self.preferences.set_autostart(False)
            self.connection.start(jid, password, host or None, port)
        except (ValueError, RuntimeError, OSError, GLib.Error) as exc:
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
            self._set_chat_actions(True)
            self.stack.set_visible_child_name("chat")
            self.peer = None
            self.peer_states.clear()
            self._own_state = None
            self.unread.clear()
            self._update_tray_count()
            self.contacts = {peer: (peer, "offline", ()) for peer in self.history.peers(data)}
            self._refresh_contacts()
        elif event == "roster":
            self.contacts.update(data)
            if self.connected:
                self._refresh_contacts()
        elif event == "message":
            peer, body, stanza_id = data
            if not self.account:
                return False
            self.history.add(self.account, peer, "in", body, stanza_id=stanza_id or None)
            self._ensure_peer(peer)
            if self.peer == peer:
                self._render_conversation()
                if self.window.is_active():
                    self._mark_read(peer)
            if not self.window.is_active() or self.peer != peer:
                self._increment_unread(peer)
            if not self.window.is_active():
                self._notify(peer, body)
        elif event == "attention":
            peer = data
            self._ensure_peer(peer)
            if self.peer == peer:
                self.header.set_subtitle(f"{peer} просит обратить внимание")
            if not self.window.is_active() or self.peer != peer:
                self._increment_unread(peer)
            if not self.window.is_active():
                self.window.set_urgency_hint(True)
                self._notify(peer, "Просит обратить внимание", urgent=True)
        elif event == "delivery":
            peer, stanza_id, status = data
            updated = self.history.set_status(self.account, peer, stanza_id, status)
            if updated and self.peer == updated[0] and stanza_id in self.message_rows:
                self._update_delivery_label(self.message_rows[stanza_id], updated[1])
        elif event == "file_received":
            peer, filename, contents, stanza_id = data
            try:
                path, expires = self.files.save(filename, contents)
            except (ValueError, OSError) as exc:
                self.header.set_subtitle(f"Ошибка вложения: {exc}")
                return False
            self.history.add(self.account, peer, "in", Path(filename).name,
                             stanza_id=stanza_id, kind="file", attachment=str(path),
                             expires_at=expires)
            self._ensure_peer(peer)
            if self.peer == peer:
                self._render_conversation()
                if self.window.is_active():
                    self._mark_read(peer)
            if not self.window.is_active() or self.peer != peer:
                self._increment_unread(peer)
            if not self.window.is_active():
                self._notify(peer, f"Файл: {filename}")
        elif event == "file_error":
            self.header.set_subtitle(data)
        elif event == "chatstate":
            peer, state = data
            self.peer_states[peer] = state
            if self.peer == peer:
                self._display_chatstate(state)
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
            self._set_chat_actions(False)
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

    def _choose_peer(self, title):
        peers = sorted(set(self.contacts) | set(self.history.peers(self.account)))
        if not peers:
            self.header.set_subtitle("Нет переписок для выбора")
            return None
        dialog = Gtk.Dialog(title=title, transient_for=self.window, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Выбрать", Gtk.ResponseType.OK)
        combo = Gtk.ComboBoxText()
        combo.set_margin_top(16)
        combo.set_margin_bottom(16)
        combo.set_margin_start(16)
        combo.set_margin_end(16)
        for peer in peers:
            combo.append(peer, f"{self.contacts.get(peer, (peer,))[0]} · {peer}")
        combo.set_active_id(self.peer if self.peer in peers else peers[0])
        dialog.get_content_area().add(combo)
        dialog.show_all()
        result = combo.get_active_id() if dialog.run() == Gtk.ResponseType.OK else None
        dialog.destroy()
        return result

    def _show_history(self, *_args):
        peer = self._choose_peer("История переписки")
        if not peer:
            return
        dialog = Gtk.Dialog(title=f"История · {peer}", transient_for=self.window, modal=True)
        dialog.set_default_size(710, 510)
        dialog.add_button("Закрыть", Gtk.ResponseType.CLOSE)
        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_border_width(12)
        search = Gtk.SearchEntry(placeholder_text="Поиск по тексту сообщений")
        content.pack_start(search, False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.set_left_margin(12)
        view.set_right_margin(12)
        view.set_top_margin(12)
        scroll.add(view)
        content.pack_start(scroll, True, True, 0)
        navigation = Gtk.Box(spacing=8)
        newer = Gtk.Button(label="← Новее")
        older = Gtk.Button(label="Старее →")
        position = Gtk.Label()
        navigation.pack_start(newer, False, False, 0)
        navigation.pack_start(position, True, True, 0)
        navigation.pack_end(older, False, False, 0)
        content.pack_start(navigation, False, False, 0)
        current_page = [0]

        def update(*_args):
            rows, total = self.history.page(self.account, peer, search.get_text(), current_page[0])
            page_count = max(1, (total + 49) // 50)
            lines = [
                f"[{timestamp}] {self.account if direction == 'out' else peer}: {body}"
                for direction, body, timestamp in rows
            ]
            view.get_buffer().set_text("\n\n".join(lines) if lines else "Сообщения не найдены")
            GLib.idle_add(lambda: scroll.get_vadjustment().set_value(0))
            position.set_text(f"Страница {current_page[0] + 1} из {page_count} · найдено: {total}")
            newer.set_sensitive(current_page[0] > 0)
            older.set_sensitive(current_page[0] + 1 < page_count)

        def change_page(offset):
            current_page[0] += offset
            update()

        def search_changed(entry):
            current_page[0] = 0
            update()

        newer.connect("clicked", lambda button: change_page(-1))
        older.connect("clicked", lambda button: change_page(1))
        search.connect("search-changed", search_changed)
        update()
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def _export_chat(self, *_args):
        peer = self._choose_peer("Экспорт переписки")
        if not peer:
            return
        chooser = Gtk.FileChooserDialog(
            title="Экспорт переписки", transient_for=self.window,
            action=Gtk.FileChooserAction.SAVE,
        )
        chooser.add_button("Отмена", Gtk.ResponseType.CANCEL)
        chooser.add_button("Экспортировать", Gtk.ResponseType.ACCEPT)
        chooser.set_do_overwrite_confirmation(True)
        chooser.set_current_name(f"Quark-{peer.replace('@', '_')}.txt")
        txt = Gtk.FileFilter()
        txt.set_name("Текст UTF-8 (*.txt)")
        txt.add_pattern("*.txt")
        csv_filter = Gtk.FileFilter()
        csv_filter.set_name("CSV UTF-8 (*.csv)")
        csv_filter.add_pattern("*.csv")
        chooser.add_filter(txt)
        chooser.add_filter(csv_filter)
        chooser.set_filter(txt)

        def sync_export_name(widget, property_spec):
            name = chooser.get_current_name()
            if name and Path(name).suffix.lower() in (".txt", ".csv"):
                suffix = ".csv" if chooser.get_filter() == csv_filter else ".txt"
                if Path(name).suffix.lower() != suffix:
                    chooser.set_current_name(str(Path(name).with_suffix(suffix)))

        chooser.connect("notify::filter", sync_export_name)
        if chooser.run() == Gtk.ResponseType.ACCEPT:
            path = Path(chooser.get_filename())
            suffix = ".csv" if chooser.get_filter() == csv_filter else ".txt"
            if path.suffix.lower() != suffix:
                path = path.with_suffix(suffix) if path.suffix.lower() in (".txt", ".csv") else path.with_name(path.name + suffix)
            try:
                if path.exists() and path != Path(chooser.get_filename()):
                    confirm = Gtk.MessageDialog(
                        transient_for=self.window, modal=True,
                        message_type=Gtk.MessageType.QUESTION,
                        buttons=Gtk.ButtonsType.YES_NO,
                        text=f"Перезаписать файл {path.name}?",
                    )
                    overwrite = confirm.run() == Gtk.ResponseType.YES
                    confirm.destroy()
                    if not overwrite:
                        chooser.destroy()
                        return
                self.history.export(self.account, peer, path)
                self.header.set_subtitle(f"История сохранена: {path.name}")
            except OSError as exc:
                self._show_error(f"Не удалось сохранить историю: {exc}")
        chooser.destroy()

    def _broadcast(self, *_args):
        dialog = Gtk.Dialog(title="Рассылка", transient_for=self.window, modal=True)
        dialog.set_default_size(480, 450)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Отправить", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)
        content.pack_start(Gtk.Label(label="Получатели", xalign=0), False, False, 0)
        contacts_scroll = Gtk.ScrolledWindow()
        contacts_scroll.set_size_request(-1, 140)
        checklist = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        choices = {}
        for jid, (name, status, groups) in sorted(self.contacts.items()):
            check = Gtk.CheckButton(label=f"{name} · {jid}")
            checklist.pack_start(check, False, False, 0)
            choices[jid] = check
        contacts_scroll.add(checklist)
        content.pack_start(contacts_scroll, False, False, 0)
        additional = Gtk.Entry(placeholder_text="Дополнительные JID через запятую (необязательно)")
        content.pack_start(additional, False, False, 0)
        content.pack_start(Gtk.Label(label="Сообщение", xalign=0), False, False, 0)
        message_scroll = Gtk.ScrolledWindow()
        message_scroll.set_min_content_height(110)
        text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        message_scroll.add(text_view)
        content.pack_start(message_scroll, True, True, 0)
        error = Gtk.Label(xalign=0)
        content.pack_start(error, False, False, 0)
        dialog.show_all()
        while dialog.run() == Gtk.ResponseType.OK:
            buffer = text_view.get_buffer()
            body = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).strip()
            try:
                peers = {jid for jid, check in choices.items() if check.get_active()}
                peers.update(bare_jid(jid) for jid in re.split(r"[,;\s]+", additional.get_text().strip()) if jid)
                if len(peers) < 2:
                    raise ValueError("Выберите не менее двух получателей")
                if not body:
                    raise ValueError("Введите сообщение")
                if not self.connected:
                    raise RuntimeError("Нет подключения к серверу")
                outgoing = {peer: uuid.uuid4().hex for peer in peers}
                self.connection.send_messages(sorted(outgoing.items()), body)
            except (ValueError, RuntimeError) as exc:
                error.set_text(str(exc))
                continue
            for peer in peers:
                self.history.add(self.account, peer, "out", body,
                                 stanza_id=outgoing[peer], status="pending")
                self._ensure_peer(peer)
                if self.peer == peer:
                    self._render_conversation()
            self.header.set_subtitle(f"Рассылка: {len(peers)} получателей")
            break
        dialog.destroy()

    def _about(self, *_args):
        dialog = Gtk.AboutDialog(
            transient_for=self.window, modal=True, program_name="Quark",
            version=__version__, authors=["Finkipp", "GPT-6 Sol"],
            website="https://github.com/Finkipp/Quark",
            license_type=Gtk.License.MIT_X11,
        )
        dialog.set_logo_icon_name("org.example.Quark")
        dialog.run()
        dialog.destroy()

    def _show_error(self, message):
        dialog = Gtk.MessageDialog(
            transient_for=self.window, modal=True,
            message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
            text=message,
        )
        dialog.run()
        dialog.destroy()

    def _notify(self, peer, text, urgent=False):
        notification = Gio.Notification.new(self.contacts.get(peer, (peer,))[0])
        notification.set_body(text)
        notification.set_default_action_and_target("app.open-chat", GLib.Variant("s", peer))
        if urgent:
            notification.set_priority(Gio.NotificationPriority.URGENT)
        self.send_notification(peer, notification)

    def _open_notification(self, action, parameter):
        peer = parameter.get_string()
        self._show_window()
        self._ensure_peer(peer)
        self._open_peer(peer)

    def _refresh_contacts(self):
        selected = self.peer
        self.contact_rows = {}
        self._updating_contacts = True
        for row in self.contact_list.get_children():
            self.contact_list.remove(row)
        grouped = {}
        for jid, (name, status, groups) in self.contacts.items():
            for group in groups or ("Без группы",):
                grouped.setdefault(group, []).append((jid, name, status))
        for group in sorted(grouped, key=str.casefold):
            heading = Gtk.ListBoxRow()
            heading.set_selectable(False)
            heading.set_activatable(False)
            title = Gtk.Label(label=group, xalign=0)
            title.set_margin_start(12)
            title.set_margin_top(12)
            title.get_style_context().add_class("heading")
            heading.add(title)
            self.contact_list.add(heading)
            for jid, name, status in sorted(grouped[group], key=lambda item: item[1].casefold()):
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
                self.contact_rows.setdefault(jid, []).append(row)
                self.contact_list.add(row)
                if jid == selected and self.contact_list.get_selected_row() is None:
                    self.contact_list.select_row(row)
        self.contact_list.show_all()
        for jid in self.contact_rows:
            self._update_unread_badge(jid)
        self._updating_contacts = False

    def _update_unread_badge(self, peer):
        for row in self.contact_rows.get(peer, ()):
            count = self.unread.get(peer, 0)
            row.badge.set_text("99+" if count > 99 else str(count))
            row.badge.set_visible(count > 0)

    def _increment_unread(self, peer):
        self.unread[peer] = self.unread.get(peer, 0) + 1
        self._update_unread_badge(peer)
        self._update_tray_count()

    def _mark_read(self, peer):
        if peer and self.unread.pop(peer, None):
            self._update_unread_badge(peer)
            self._update_tray_count()
        if peer:
            self.withdraw_notification(peer)
            if self.connected and self.window.is_active():
                ids = self.history.mark_read(self.account, peer)
                if ids:
                    try:
                        self.connection.mark_read(peer, ids[-1])
                    except RuntimeError:
                        pass

    def _window_active_changed(self, window, property_spec):
        if window.is_active():
            self._mark_read(self.peer)
            window.set_urgency_hint(False)
            self._send_chatstate("active")
        else:
            self._send_chatstate("inactive")

    def _display_chatstate(self, state):
        self.typing_label.set_text({
            "composing": "печатает сообщение…",
            "paused": "перестал печатать",
            "inactive": "неактивен в чате",
            "gone": "покинул чат",
        }.get(state, ""))

    def _send_chatstate(self, state):
        if self.connected and self.peer and self._own_state != (self.peer, state):
            try:
                self.connection.send_chatstate(self.peer, state)
                self._own_state = (self.peer, state)
            except RuntimeError:
                pass

    def _composer_changed(self, buffer):
        if self._typing_timer:
            GLib.source_remove(self._typing_timer)
            self._typing_timer = None
        if not self.peer or not self.window or not self.window.is_active():
            return
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        self._send_chatstate("composing" if text.strip() else "active")
        if text.strip():
            self._typing_timer = GLib.timeout_add_seconds(5, self._typing_paused)

    def _typing_paused(self):
        self._typing_timer = None
        self._send_chatstate("paused")
        return False

    def _ensure_peer(self, peer):
        if peer not in self.contacts:
            self.contacts[peer] = (peer, "offline", ())
            self._refresh_contacts()

    def _select_row(self, listbox, row):
        if row and not self._updating_contacts and row.jid != self.peer:
            self._open_peer(row.jid)

    def _open_peer(self, peer):
        if self.peer and self.peer != peer:
            self._send_chatstate("inactive")
        self.peer = peer
        self._own_state = None
        self._send_chatstate("active")
        self._display_chatstate(self.peer_states.get(peer, ""))
        self.peer_label.set_text(self.contacts.get(peer, (peer,))[0])
        self.peer_jid.set_text(peer)
        rows = self.contact_rows.get(peer, ())
        row = rows[0] if rows else None
        if row and self.contact_list.get_selected_row() != row:
            self.contact_list.select_row(row)
        self.visible_limit = 100
        self.search_entry.set_text("")
        self.search_bar.set_search_mode(False)
        self._render_conversation()
        self._mark_read(peer)
        self.window.set_urgency_hint(False)
        self.message_entry.grab_focus()

    def _render_conversation(self, scroll_to_bottom=True):
        if not self.peer or not self.account:
            return
        for child in self.message_column.get_children():
            self.message_column.remove(child)
        self.message_rows = {}
        query = self.search_entry.get_text() if self.search_bar.get_search_mode() else ""
        rows, total = self.history.conversation(
            self.account, self.peer, query, page_size=self.visible_limit
        )
        if total > self.visible_limit:
            older = Gtk.Button(label=f"Показать более ранние · осталось {total - self.visible_limit}")
            older.get_style_context().add_class("flat")
            older.connect("clicked", lambda button: self._load_older())
            self.message_column.pack_start(older, False, False, 0)
        if not rows:
            label = Gtk.Label(label="Сообщений не найдено" if query else "Пока нет сообщений")
            label.get_style_context().add_class("dim-label")
            self.message_column.pack_start(label, False, False, 0)
        last_day = None
        for row_id, direction, body, timestamp, stanza_id, status, attachment, kind in rows:
            day = timestamp[:10]
            if day != last_day:
                date = Gtk.Label(label=day)
                date.get_style_context().add_class("dim-label")
                self.message_column.pack_start(date, False, False, 2)
                last_day = day
            outer = Gtk.Box()
            bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            bubble.get_style_context().add_class("message-out" if direction == "out" else "message-in")
            bubble.set_size_request(90, -1)
            title = Gtk.Label(label=("Вы" if direction == "out" else self.contacts.get(self.peer, (self.peer,))[0]), xalign=0)
            title.get_style_context().add_class("dim-label")
            bubble.pack_start(title, False, False, 0)
            if kind == "file":
                bubble.pack_start(self._file_widget(body, attachment), False, False, 0)
            else:
                bubble.pack_start(render_message(body), False, False, 0)
            meta = Gtk.Box(spacing=5)
            clock = Gtk.Label(label=timestamp[11:16])
            clock.get_style_context().add_class("dim-label")
            meta.pack_start(clock, False, False, 0)
            if direction == "out" and stanza_id:
                indicator = Gtk.Label()
                self._update_delivery_label(indicator, status)
                meta.pack_end(indicator, False, False, 0)
                self.message_rows[stanza_id] = indicator
            bubble.pack_start(meta, False, False, 0)
            if direction == "out":
                outer.pack_end(bubble, False, False, 0)
            else:
                outer.pack_start(bubble, False, False, 0)
            self.message_column.pack_start(outer, False, False, 0)
        self.message_column.show_all()
        if scroll_to_bottom:
            GLib.idle_add(lambda: self.messages_scroll.get_vadjustment().set_value(
                max(0, self.messages_scroll.get_vadjustment().get_upper() -
                    self.messages_scroll.get_vadjustment().get_page_size())))

    def _load_older(self):
        self.visible_limit += 100
        self._render_conversation(scroll_to_bottom=False)

    @staticmethod
    def _update_delivery_label(label, status):
        label.set_text({
            "pending": "⋯", "delivered": "✓", "read": "✓✓", "failed": "!",
        }.get(status, ""))
        label.set_tooltip_text({
            "pending": "Ожидание подтверждения доставки", "delivered": "Доставлено",
            "read": "Прочитано", "failed": "Не доставлено",
        }.get(status, "Статус неизвестен"))
        context = label.get_style_context()
        if status == "failed":
            context.add_class("message-failed")
        else:
            context.remove_class("message-failed")

    def _file_widget(self, filename, attachment):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        path = Path(attachment) if attachment else None
        if path and path.is_file():
            if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                try:
                    image = Gtk.Image.new_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(
                        str(path), 320, 220, True
                    ))
                    content.pack_start(image, False, False, 0)
                except GLib.Error:
                    pass
            button = Gtk.Button(label=f"📎 {filename} · открыть")
            button.connect("clicked", lambda widget: self._open_attachment(path))
            content.pack_start(button, False, False, 0)
        else:
            label = Gtk.Label(label=f"📎 {filename} · удалено через 24 часа")
            label.get_style_context().add_class("dim-label")
            content.pack_start(label, False, False, 0)
        return content

    def _open_attachment(self, path):
        try:
            Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        except GLib.Error as exc:
            self._show_error(f"Не удалось открыть вложение: {exc}")

    def _send(self, *_args):
        buffer = self.message_entry.get_buffer()
        body = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True).strip()
        if not body or not self.peer:
            return
        try:
            if not self.connected:
                raise RuntimeError("Нет подключения к серверу")
            stanza_id = uuid.uuid4().hex
            self.connection.send_message(self.peer, body, stanza_id)
        except RuntimeError as exc:
            self.header.set_subtitle(str(exc))
            return
        self.history.add(self.account, self.peer, "out", body, stanza_id=stanza_id, status="pending")
        self._render_conversation()
        buffer.set_text("")

    def _composer_key(self, widget, event):
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        if ctrl and event.keyval in (Gdk.KEY_v, Gdk.KEY_V):
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            if clipboard.wait_is_image_available():
                image = clipboard.wait_for_image()
                if image:
                    self._paste_image(image)
                    return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not shift:
            self._send()
            return True
        if ctrl and event.keyval in (Gdk.KEY_b, Gdk.KEY_B):
            self._wrap_selection("**", "**")
            return True
        if ctrl and event.keyval in (Gdk.KEY_i, Gdk.KEY_I):
            self._wrap_selection("*", "*")
            return True
        return False

    def _attach_file(self, *_args):
        if not self.connected or not self.peer:
            self.header.set_subtitle("Сначала откройте чат и подключитесь к серверу")
            return
        dialog = Gtk.FileChooserDialog(
            title="Прикрепить файл", transient_for=self.window,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Отправить", Gtk.ResponseType.ACCEPT)
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            path = Path(dialog.get_filename())
            try:
                if not 0 < path.stat().st_size <= MAX_FILE:
                    raise ValueError("Размер файла должен быть от 1 байта до 5 МБ")
                self._send_attachment(path.name, path.read_bytes())
            except (ValueError, OSError) as exc:
                self._show_error(str(exc))
        dialog.destroy()

    def _paste_image(self, image):
        try:
            success, content = image.save_to_bufferv("png", [], [])
            if not success:
                raise ValueError("Не удалось прочитать изображение из буфера обмена")
            self._send_attachment("изображение.png", bytes(content))
        except (ValueError, OSError, GLib.Error) as exc:
            self._show_error(str(exc))

    def _send_attachment(self, filename, data):
        if not self.connected or not self.peer:
            raise ValueError("Нет активного чата")
        path, expires = self.files.save(filename, data)
        sid = uuid.uuid4().hex
        try:
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            self.connection.send_file(self.peer, data, filename, mime, sid)
        except RuntimeError:
            path.unlink(missing_ok=True)
            raise ValueError("Нет подключения к серверу") from None
        self.history.add(self.account, self.peer, "out", filename, stanza_id=sid,
                         status="pending", kind="file", attachment=str(path), expires_at=expires)
        self._render_conversation()

    def _wrap_selection(self, before, after, placeholder=""):
        buffer = self.message_entry.get_buffer()
        bounds = buffer.get_selection_bounds()
        if bounds:
            start, end = bounds
            selected = buffer.get_text(start, end, True)
            offset = start.get_offset()
            buffer.delete(start, end)
            buffer.insert(buffer.get_iter_at_offset(offset), before + selected + after)
        else:
            offset = buffer.get_iter_at_mark(buffer.get_insert()).get_offset()
            buffer.insert(buffer.get_iter_at_offset(offset), before + placeholder + after)
            buffer.place_cursor(buffer.get_iter_at_offset(offset + len(before)))
        self.message_entry.grab_focus()

    def _composer_menu(self, widget, menu):
        menu.append(Gtk.SeparatorMenuItem())
        for label, before, after in (
            ("Жирный", "**", "**"), ("Курсив", "*", "*"),
            ("Однострочный код", "`", "`"), ("Блок кода", "```\n", "\n```"),
            ("Ссылка", "[", "](https://example.org)"),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda button, b=before, a=after: self._wrap_selection(b, a))
            menu.append(item)
        menu.show_all()

    def _send_attention(self, *_args):
        if not self.peer:
            return
        try:
            if not self.connected:
                raise RuntimeError("Нет подключения к серверу")
            self.connection.send_attention(self.peer)
            self.header.set_subtitle("Вы попросили обратить внимание")
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
        self._set_chat_actions(False)
        self.header.set_subtitle(None)
        self.connection.stop()
        self.login_button.set_sensitive(True)
        self.stack.set_visible_child_name("login")

    def _on_close(self, *_args):
        if self.tray and not (isinstance(self.tray, Gtk.StatusIcon) and not self.tray.is_embedded()):
            self._send_chatstate("inactive")
            self.hold()
            self._held_in_tray = True
            self.window.hide()
            return True
        self._quit_app()
        return True

    def _quit_app(self, *_args):
        self._send_chatstate("gone")
        self.connection.stop()
        if self.history:
            self.history.close()
            self.history = None
        if self._indicator_status:
            self.tray.set_status(self._indicator_status.PASSIVE)
        elif self.tray:
            self.tray.set_visible(False)
        if self._held_in_tray:
            self.release()
            self._held_in_tray = False
        self.quit()


def main():
    return ChatApplication().run(sys.argv)
