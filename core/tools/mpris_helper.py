#!/usr/bin/python3
"""Плеер «Ксения» на шине сеанса (MPRIS) — чтобы касания наушников доходили до Ксении.

Запускается СИСТЕМНЫМ python3 (там есть gi/Gio). Касание наушника по классическому Bluetooth (AVRCP) BlueZ
превращает в медиаклавишу, KDE отдаёт её активному MPRIS-плееру (или mpris-proxy — напрямую); LE Audio
(служба Media Control в BlueZ, пока экспериментальная) тоже переводит команды в вызовы MPRIS. Своего GATT-сервера
не нужно: достаточно быть MPRIS-плеером.

Обмен с ядром — JSON-строки:
  stdout: {"ready": true} | {"event": "PlayPause"} | {"event": "Seek", "offset": мкс} | {"error": "..."}
  stdin:  {"status": "Playing"|"Paused"|"Stopped", "title": "..."} — что показывать и что слышат кнопки.
Конец stdin (ядро закрылось) — помощник выходит.
"""
import json
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

BUS_NAME = "org.mpris.MediaPlayer2.ksenia"
PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
TRACK = "/org/mpris/MediaPlayer2/ksenia/track/1"

XML = f"""<node>
  <interface name="{ROOT_IFACE}">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="{PLAYER_IFACE}">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/><arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Metadata" type="a{{sv}}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>"""

STATE = {"status": "Stopped", "title": "Ксения"}
EVENTS = {"Next", "Previous", "Pause", "PlayPause", "Stop", "Play"}


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def metadata():
    return GLib.Variant("a{sv}", {"mpris:trackid": GLib.Variant("o", TRACK),
                                  "xesam:title": GLib.Variant("s", STATE["title"]),
                                  "xesam:artist": GLib.Variant("as", ["Ксения"])})


def prop(iface, name):
    if iface == ROOT_IFACE:
        values = {"CanQuit": ("b", False), "CanRaise": ("b", False), "HasTrackList": ("b", False),
                  "Identity": ("s", "Ксения"), "SupportedUriSchemes": ("as", []), "SupportedMimeTypes": ("as", [])}
    else:
        if name == "Metadata":
            return metadata()
        # «можно всё»: иначе KDE и BlueZ не отдают плееру кнопки (CanPlay=false — кнопка серая)
        values = {"PlaybackStatus": ("s", STATE["status"]), "Rate": ("d", 1.0), "Volume": ("d", 1.0),
                  "Position": ("x", 0), "MinimumRate": ("d", 1.0), "MaximumRate": ("d", 1.0),
                  "CanGoNext": ("b", True), "CanGoPrevious": ("b", True), "CanPlay": ("b", True),
                  "CanPause": ("b", True), "CanSeek": ("b", True), "CanControl": ("b", True)}
    if name not in values:
        return None
    sig, val = values[name]
    return GLib.Variant(sig, val)


def on_method(conn, sender, path, iface, method, params, invocation):
    if iface == PLAYER_IFACE and method in EVENTS:
        out({"event": method})
    elif iface == PLAYER_IFACE and method == "Seek":
        out({"event": "Seek", "offset": params.unpack()[0]})  # LE Audio: перемотка вперёд/назад
    invocation.return_value(None)


def on_get(conn, sender, path, iface, name):
    return prop(iface, name)


def on_set(conn, sender, path, iface, name, value):
    return True  # громкость и скорость — не наши: принимаем и молча игнорируем


def main():
    node = Gio.DBusNodeInfo.new_for_xml(XML)
    loop = GLib.MainLoop()
    holder = {}

    def changed():
        conn = holder.get("conn")
        if conn is None:
            return
        body = GLib.Variant("(sa{sv}as)", (PLAYER_IFACE, {"PlaybackStatus": GLib.Variant("s", STATE["status"]),
                                                          "Metadata": metadata()}, []))
        conn.emit_signal(None, PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged", body)

    def on_bus(conn, name):
        holder["conn"] = conn
        register = getattr(conn, "register_object_with_closures", None) or conn.register_object
        for iface in node.interfaces:
            register(PATH, iface, on_method, on_get, on_set)

    def on_acquired(conn, name):
        out({"ready": True, "name": name})

    def on_lost(conn, name):
        out({"error": "name_lost"})
        loop.quit()

    def on_stdin(channel, cond):
        if cond & (GLib.IOCondition.HUP | GLib.IOCondition.ERR) and not cond & GLib.IOCondition.IN:
            loop.quit()
            return False
        line = sys.stdin.readline()
        if not line:
            loop.quit()
            return False
        try:
            msg = json.loads(line)
        except ValueError:
            return True
        status = msg.get("status")
        title = str(msg.get("title") or "Ксения")[:200]
        if status in ("Playing", "Paused", "Stopped") and (status, title) != (STATE["status"], STATE["title"]):
            STATE.update(status=status, title=title)
            changed()
        return True

    # REPLACE: зависший прошлый помощник уступит имя новому
    flags = Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT | Gio.BusNameOwnerFlags.REPLACE
    owner = Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, flags, on_bus, on_acquired, on_lost)
    GLib.io_add_watch(GLib.IOChannel.unix_new(sys.stdin.fileno()), GLib.PRIORITY_DEFAULT,
                      GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR, on_stdin)
    try:
        loop.run()
    finally:
        Gio.bus_unown_name(owner)


if __name__ == "__main__":
    main()
