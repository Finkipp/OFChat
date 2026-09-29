"""Small GTK desktop client for one-to-one XMPP chats."""

import re
import sys
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from .storage import History
from .version import __version__
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
            ("history", self._show_history), ("export-chat", self._export_chat),
            ("about", self._about), ("quit-ofchat", self._quit_app),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if name in ("disconnect", "broadcast", "history", "export-chat"):
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
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), styles, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.history = History()

    def do_activate(self):
        if self.window:
            self._show_window()
            return
        self.window = Gtk.ApplicationWindow(application=self, title="OFChat")
        self.window.set_default_size(850, 580)
        self.window.set_icon_name("mail-message-new")
        self.window.connect("delete-event", self._on_close)
        self.window.connect("notify::is-active", self._window_active_changed)
        self.header = Gtk.HeaderBar(title="OFChat", show_close_button=True)
        menu = Gio.Menu()
        files = Gio.Menu()
        files.append("Экспорт переписки…", "app.export-chat")
        actions = Gio.Menu()
        actions.append("Рассылка…", "app.broadcast")
        actions.append("История…", "app.history")
        menu.append_submenu("Файл", files)
        menu.append_submenu("Действия", actions)
        menu.append("О программе", "app.about")
        menu.append("Отключиться", "app.disconnect")
        menu.append("Выйти из OFChat", "app.quit-ofchat")
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
        self._build_tray()

    def _build_tray(self):
        menu = Gtk.Menu()
        self.tray_open = Gtk.MenuItem(label="Показать OFChat")
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
                APP_ID, "mail-message-new", AppIndicator3.IndicatorCategory.COMMUNICATIONS
            )
            indicator.set_menu(menu)
            indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self._indicator_status = AppIndicator3.IndicatorStatus
            self.tray = indicator
        except (ImportError, ValueError):
            # Traditional notification areas still support Gtk.StatusIcon.
            icon = Gtk.StatusIcon.new_from_icon_name("mail-message-new")
            icon.set_tooltip_text("OFChat")
            icon.connect("activate", self._toggle_window)
            icon.connect("popup-menu", lambda widget, button, time: menu.popup(
                None, None, Gtk.StatusIcon.position_menu, widget, button, time
            ))
            self.tray = icon

    def _show_window(self, *_args):
        self.window.present()
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
            self.tray.set_tooltip_text(f"OFChat — непрочитанных: {total}" if total else "OFChat")

    def _set_chat_actions(self, enabled):
        for name in ("disconnect", "broadcast", "history", "export-chat"):
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
        preview_note = Gtk.Label(label="Последние 3 дня · до 100 сообщений. Вся переписка: Действия → История", xalign=0)
        preview_note.get_style_context().add_class("dim-label")
        preview_note.set_ellipsize(Pango.EllipsizeMode.END)
        right.pack_start(preview_note, False, False, 0)
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
            self._set_chat_actions(True)
            self.stack.set_visible_child_name("chat")
            self.peer = None
            self.unread.clear()
            self._update_tray_count()
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
        chooser.set_current_name(f"OFChat-{peer.replace('@', '_')}.txt")
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
        for jid, (name, status) in sorted(self.contacts.items()):
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
                self.connection.send_messages(sorted(peers), body)
            except (ValueError, RuntimeError) as exc:
                error.set_text(str(exc))
                continue
            for peer in peers:
                self.history.add(self.account, peer, "out", body)
                self._ensure_peer(peer)
                if self.peer == peer:
                    self._append("out", body)
            self.header.set_subtitle(f"Рассылка: {len(peers)} получателей")
            break
        dialog.destroy()

    def _about(self, *_args):
        dialog = Gtk.AboutDialog(
            transient_for=self.window, modal=True, program_name="OFChat",
            version=__version__, authors=["Finkipp", "GPT-6 Sol"],
            website="https://github.com/Finkipp/OFChat",
            license_type=Gtk.License.MIT_X11,
        )
        dialog.set_logo_icon_name("mail-message-new")
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
        self._update_tray_count()

    def _mark_read(self, peer):
        if peer and self.unread.pop(peer, None):
            self._update_unread_badge(peer)
            self._update_tray_count()
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
        self._set_chat_actions(False)
        self.header.set_subtitle(None)
        self.connection.stop()
        self.login_button.set_sensitive(True)
        self.stack.set_visible_child_name("login")

    def _on_close(self, *_args):
        if self.tray and not (isinstance(self.tray, Gtk.StatusIcon) and not self.tray.is_embedded()):
            self.hold()
            self._held_in_tray = True
            self.window.hide()
            return True
        self._quit_app()
        return True

    def _quit_app(self, *_args):
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
