#!/usr/bin/env python3
# This file is part of BuhIRC.
# 
# BuhIRC is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# BuhIRC is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the#  GNU General Public License
# along with BuhIRC.  If not, see <http://www.gnu.org/licenses/>.
import sys
import json
import base64
import logging
import requests

from line import Line
from database import Database
from modules import ModuleManager
from collections import defaultdict
from permissions import Permissions
from connection import IRCConnection
from hooks import HookManager, Hook


class BuhIRC:
    def __init__(self, config_file):
        self.config_file = config_file
        self.config = json.load(open(self.config_file))
        self.me = self.config["me"]
        self.net = self.config["network"]
        self.module_manager = ModuleManager(self)
        self.hook_manager = HookManager(self)
        self.perms = Permissions(self)
        self.connection = IRCConnection(self.net["address"], self.net["port"], self.net["ssl"], self.config["proxies"].get(self.net.get("proxy", "none"), None), self.net.get("flood_interval", 0.0))
        self.running = True
        self.state = {}  # Dict used to hold stuff like last line received and last message etc...
        self.db = Database("etc/buhirc.db")
        self.db.connect()
        logging.basicConfig(level=getattr(logging, self.config["misc"]["loglevel"]), format='[%(asctime)s] %(levelname)s: %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')
        self.requests_session = requests.session()
        if self.config["misc"].get("http_proxy", "none") != "none":
            proxy = self.config["proxies"].get(self.config["misc"]["http_proxy"], "none")
            if proxy != "none":
                self.requests_session.proxies = {"http": proxy, "https": proxy}

        self.flood_verbs = [x.lower() for x in self.net.get("flood_verbs", [])]
        self.help = {}

    def run(self):
        self.connection.connect()
        if self.config["network"]["sasl"]["use"]:
            self.raw("CAP REQ :sasl")
        self.raw("NICK %s" % self.me["nicks"][0])  # Nicks thing is a temp hack
        self.raw("USER %s * * :%s" % (self.me["ident"], self.me["gecos"]))

        for module in self.config["modules"]:
            self.module_manager.load_module(module)

        while self.running:
            if not self.loop():
                self.stop()

    def raw(self, line):
        """
        Send a raw IRC line to the server.
        @param line: The raw line to send, without a trailing carriage return or newline.
        """
        logging.debug("[IRC] <- %s" % line)
        ln = Line.parse(line)
        force = True  # Whether we bypass flood protection or not.
        if ln.command.lower() in self.flood_verbs:
            force = False

        self.connection.write_line(line, force)

    def parse_line(self, ln):
        logging.debug("[IRC] -> %s" % ln.linestr)
        if ln.command == "PING":
            self.raw(ln.linestr.replace("PING", "PONG"))
        elif ln.command == "376":
            for channel in self.net["channels"]:
                self.join(channel)
        elif ln.command == "CAP":
            if ln.params[1] == "ACK" and ln.params[-1] == "sasl" and self.net["sasl"]["use"]:
                self.raw("AUTHENTICATE PLAIN")
        elif ln.command == "AUTHENTICATE":
            magic = "%s\x00%s\x00%s" % (self.net["sasl"]["username"], self.net["sasl"]["username"], self.net["sasl"]["password"])
            magic = base64.b64encode(magic.encode("ascii"))
            self.raw("AUTHENTICATE %s" % magic)
        elif ln.command == "903":
            self.raw("CAP END")
        elif ln.command == "904":
            logging.warning("SASL authentication failed, continuing login anyways...")
            self.raw("CAP END")

    def loop(self):
        if not self.connection.loop():
            return False

        for line in self.connection.buffer:
            ln = Line.parse(line)
            self.state["last_line"] = ln
            self.parse_line(ln)
            self.hook_manager.run_irc_hooks(ln)

        return True

    def stop(self):
        self.raw("QUIT :Bye!")
        self.connection.disconnect()
        self.running = False

    def rehash(self):
        """
        Rehash (reread and reparse) the bot's configuration file.
        """
        self.config = json.load(open(self.config_file))

    # Helper functions
    def hook_command(self, cmd, callback, help_text=None):
        """
        Register a command hook to the bot.
        @param cmd: Command name to hook.
        @param callback: Event callback function to call when this command is ran.
        @param help_text: Help text for this command, no help if not specified.
        @return: ID of the new hook. (Used for removal later)
        """
        cmd = cmd.lower()

        if help_text:
            self.help[cmd] = help_text

        return self.hook_manager.add_hook(Hook("command_%s" % cmd, callback))

    def hook_numeric(self, numeric, callback):
        """
        Register a raw numeric hook to the bot.
        @param numeric: The raw IRC numeric (or command, such as PRIVMSG) to hook.
        @param callback: Event callback function to call when this numeric/command is received from the server.
        @return: ID of the new hook. (Used for removal later)
        """
        return self.hook_manager.add_hook(Hook("irc_raw_%s" % numeric, callback))

    def list_commands(self):
        """
        Get a list of all commands the bot knows about.
        @return: list of command names
        """
        return [hook[8:] for hook in self.hook_manager.hooks.keys() if hook.startswith("command_")]  # len("command_") == 8

    def unhook_something(self, the_id):
        """
        Unhook any sort of hook. (Command, numeric, or event.)
        @param the_id: The ID of the hook to remove, returned by a hook adding function.
        """
        self.hook_manager.remove_hook(the_id)

    def is_admin(self, hostmask=None):
        """
        Check if a hostmask is a bot admin.
        @param hostmask: The hostmask to check.
        @return: True if admin, False if not.
        """
        if hostmask is None:
            hostmask = self.state["last_line"].hostmask

        return self.perms.check_permission(hostmask, "admin")

    def check_condition(self, condition, false_message="Sorry, you may not do that.", reply_func=None):
        """
        Check a condition and return it, calling reply_func with false_message if the condition is False.
        @param condition: The condition to check.
        @param false_message: The message to be passed to reply_func
        @param reply_func: The function to call with false_message as argument if condition is False.
        @return:
        """
        if reply_func is None:
            reply_func = self.reply

        if condition:
            return True

        reply_func(false_message)
        return False

    def check_permission(self, permission="admin", error_reply="Sorry, you do not have permission to do that!",
                         reply_func=None):
        """
        Check a bot permission against the hostmask of the last line received, and return whether it matches.
        Calls reply_func with error_reply as argument if condition is False
        @param permission: The permission to check.
        @param error_reply: The message to be passed to reply_func
        @param reply_func: The function to call with error_reply as argument if condition is False.
        @return:
        """
        if reply_func is None:
            reply_func = self.reply_notice

        return self.check_condition(self.perms.check_permission(self.state["last_line"].hostmask, permission),
                                    error_reply, reply_func)

    # IRC-related stuff begins here
    def _msg_like(self, verb, target, message):
        self.raw("%s %s :%s" % (verb, target, message))

    def privmsg(self, target, message):
        """
        Send a PRIVMSG (channel or user message) to a user/channel.
        @param target: The target to send this message to. (Can be nickname or channel.)
        @param message: The actual message to send.
        """
        self._msg_like("PRIVMSG", target, message)

    def act(self, target, action):
        """
        Send a CTCP ACTION (/me) to a user/channel.
        @param target: The target to send this ACTION to. (Can be nickname or channel.)
        @param action: The actual action to send.
        """
        self.privmsg(target, "\x01ACTION %s\x01" % action)

    def notice(self, target, message):
        """
        Send a NOTICE to a user/channel.
        @param target: The user or channel to send this notice to.
        @param message: The actual notice text.
        """
        self._msg_like("NOTICE", target, message)

    def join(self, channel):
        """
        Send a raw channel JOIN message to the server. (Join a channel)
        @param channel: The channel to join. (Key can be passed in the same argument, separated by a space.)
        """
        self.raw("JOIN %s" % channel)

    def part(self, channel):
        """
        Send a raw channel PART to the server. (Leave a channel)
        @param channel: The channel to leave.
        """
        self.raw("PART %s" % channel)

    # IRC-related stuff that involves state.
    def reply(self, message):
        """
        Send a PRIVMSG (channel or user message) to the last channel or user we received a message in.
        @param message: The reply message to send.
        """
        ln = self.state["last_line"]
        reply_to = ln.hostmask.nick

        if ln.params[0][0] == "#":
            reply_to = ln.params[0]

        self.privmsg(reply_to, message)

    def reply_act(self, action):
        """
        Send a CTCP ACTION (/me) to the last channel or user we received a message in.
        @param action: The action to send.
        """
        self.reply("\x01ACTION %s\x01" % action)

    def reply_notice(self, message):
        """
        Send a NOTICE to the last channel or user we received a message in.
        @param message: The notice text to send.
        """
        ln = self.state["last_line"]
        self.notice(ln.hostmask.nick, message)

    # Web stuff.
    def http_get(self, url, **kwargs):
        """
        Perform an HTTP GET using requests.
        @param url: The URL to GET.
        @param kwargs: Any arguments to pass to requests.get()
        @return: requests.Response object.
        """
        return self.requests_session.get(url, **kwargs)

    def http_post(self, url, **kwargs):
        """
        Perform an HTTP POST using requests.
        @param url: The URL to POST to.
        @param kwargs: Any arguments to pass to requests.get()
        @return: requests.Response object.
        """
        return self.requests_session.post(url, **kwargs)

if __name__ == "__main__":
    conf = "etc/buhirc.json"
    if len(sys.argv) > 1:
        conf = sys.argv[1]

    b = BuhIRC(conf)
    try:
        b.run()
    except KeyboardInterrupt:
        logging.info("Interrupted, exiting cleanly!")
    b.stop()

