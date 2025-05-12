import socket
import json
import threading
import time
import os
import sys
import logging
import argparse

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d - %(levelname)s - %(name)s - %(threadName)s - %(message)s',
    datefmt='%H:%M:%S',
    handlers=[]
)
module_logger = logging.getLogger(__name__) # for this file

DEFAULT_SERVER_HOST = '127.0.0.1'
DEFAULT_SERVER_PORT = 4242

class StandardCLIClient:
    def __init__(self, host, port, name="Client", simulated_send_delay_ms=0):
        self.host = host
        self.port = port
        self.name = name
        self.logger = logging.getLogger(f"{__name__}.{self.name.replace(' ', '_')}") # specific logger for this client
        self.logger.setLevel(logging.DEBUG) # always log debug from client instance, console filters later
        self.client_socket = None
        self.connected = False
        self.receive_thread = None
        self.stop_receive = threading.Event()
        self.username = None
        self.current_session_id = None
        self.is_session_host = False
        self.status_message = "Not Connected" # for cli display
        self.last_rtt_ms = None

        self.ping_start_time = None # when we sent the current ping
        self.active_ping_payload = None # what data we sent with the ping

        self._pending_join_sid_cli = None # session id client tried to join, waiting for lobby update
        self.known_sessions_cache = {} # short_id -> full_id

        self.current_input_buffer = "" # for redrawing cli input
        self.input_lock = threading.Lock()

        # delays for testing simulation
        self.simulated_ping_delay_ms = 0 # specific for pings
        self.simulated_answer_delay_ms = 0 # specific for answers
        # general delay, useful for pytest to override if specific ones aren't set
        self.simulated_send_delay_ms = simulated_send_delay_ms
        self.simulated_send_delay_ms = simulated_send_delay_ms # this was a duplicate, kept it as is

        # attributes for pytest integration
        self.received_messages_log = [] # log of all messages received, for tests
        self.test_event_handler = None # tests can hook into this
        self.client_ready_for_game_event = threading.Event() # for tests, when client can start game
        self.logger.info(f"Instance '{self.name}' initialized for {self.host}:{self.port}")

        # attributes for pytest integration (kept for compatibility if tests are used)
        self.received_messages_log = [] # yeah this is duplicated too, keeping it
        self.test_event_handler = None
        self.client_ready_for_game_event = threading.Event()
        self.problem_received_event = threading.Event() # for tests, when problem arrives
        self.latest_problem_data = None # for tests
        self.latest_round_result = None # for tests
        self.latest_game_over = None # for tests

    def clear_message_log(self):
        # for testing
        if hasattr(self, 'received_messages_log'): # make sure it exists
         self.received_messages_log.clear()
         self.logger.debug("Internal received_messages_log cleared for test.")
        else:
            # this shouldn't happen if init always makes it
            self.logger.warning("Attempted to clear received_messages_log, but it doesn't exist.")
            self.received_messages_log = [] # make it if missing, just in case

    def get_last_messages(self, count=1, command_filter=None):
        # returns the last `count` messages from internal log, optionally filtered. for tests
        if not hasattr(self, 'received_messages_log'):
            self.logger.warning("get_last_messages called, but received_messages_log doesn't exist.")
            return []

        matching_messages = []
        # iterate a copy in case list is modified by receive thread
        log_copy = list(self.received_messages_log)
        for logged_msg_info in reversed(log_copy):
            msg_content = logged_msg_info["message"]
            if command_filter:
                if msg_content.get("command") == command_filter:
                    matching_messages.append(msg_content)
            else:
                matching_messages.append(msg_content)
            if len(matching_messages) >= count: # got enough
                break
        return list(reversed(matching_messages)) # return in original order

    def wait_for_message(self, command_name, timeout=5, after_timestamp=0):
        # for tests, waits for a specific command
        if not hasattr(self, 'received_messages_log'):
            self.logger.error("wait_for_message called, but received_messages_log doesn't exist. Cannot wait.")
            return None

        start_wait_perf = time.perf_counter()
        self.logger.debug(f"Waiting for '{command_name}' (timeout {timeout}s, after_ts {after_timestamp:.3f})")

        while time.perf_counter() - start_wait_perf < timeout:
            # self.received_messages_log is populated in handle_server_message if PYTEST_CURRENT_TEST is set
            log_copy = list(self.received_messages_log) # copy for safe iteration
            for logged_msg_info in reversed(log_copy): # check newest first
                msg_ts_perf = logged_msg_info["timestamp"] # this is perf_counter timestamp
                msg_content = logged_msg_info["message"]

                # debug print to see what's being checked
                # self.logger.debug(f"  Checking msg: CMD={msg_content.get('command')} @TS={msg_ts_perf:.3f} vs after_TS={after_timestamp:.3f}")

                if msg_content.get("command") == command_name and msg_ts_perf > after_timestamp:
                    self.logger.debug(f"Found '{command_name}': {str(msg_content)[:100]} at perf_ts {msg_ts_perf:.3f}")
                    return msg_content
            time.sleep(0.02) # short sleep to yield execution and avoid busy-waiting

        self.logger.warning(f"Timeout waiting for '{command_name}' after timestamp {after_timestamp:.3f}. Last few messages:")
        for i, logged_msg_info in enumerate(reversed(log_copy[-5:])): # log last 5 messages on timeout
            self.logger.warning(f"  TimeoutLog [{len(log_copy)-1-i}]: CMD={logged_msg_info['message'].get('command')} @TS={logged_msg_info['timestamp']:.3f} - Data={str(logged_msg_info['message'])[:60]}")
        return None
    def get_prompt(self):
        # makes the little > thingy for the cli
        if self.username and self.current_session_id:
            return f"{self.name}(S:{self.current_session_id[:8]})> "
        elif self.username:
          return f"{self.name}(Lobby)> "
        elif self.connected:
            return f"{self.name}(Connected)> "
        else:
          return f"{self.name}(Offline)> "

    def _print_to_cli(self, message_lines):
        """helper to print messages to cli, managing input buffer."""
        if not sys.stdin.isatty() or os.environ.get("PYTEST_CURRENT_TEST"): # if not a real terminal or in test
            return # avoid printing

        with self.input_lock:
            current_line_content = self.current_input_buffer
            # clear current line by writing spaces then CR
            # sys.stdout.write('\r' + ' ' * (len(self.get_prompt()) + len(current_line_content)) + '\r')
            sys.stdout.write('\n') # just print on a new line, simpler

            if isinstance(message_lines, str): # can be single string or list
                print(message_lines)
            else: # assume list of strings
                for line in message_lines:
                    print(line)

            # rewrite prompt and user's current input buffer
            sys.stdout.write(self.get_prompt() + current_line_content)
            sys.stdout.flush()

    def display_status_and_prompt(self):
        # shows the status bar and prompt
        if os.environ.get("PYTEST_CURRENT_TEST"): return # no display in tests
        status_bar = f"--- Status: {self.status_message} | RTT: {f'{self.last_rtt_ms:.0f}ms' if self.last_rtt_ms is not None else 'N/A'} ---"
        # this goes directly to console, not through _print_to_cli to avoid recursive lock/complex clearing
        with self.input_lock: # protect global stdout
            current_line_content = self.current_input_buffer
            sys.stdout.write('\r' + ' ' * (len(self.get_prompt()) + len(current_line_content)) + '\r') # clear line
            print(status_bar)
            sys.stdout.write(self.get_prompt()) # prompt will be displayed, user types after it
            sys.stdout.flush()


    def connect(self):
        if self.connected:
            self.logger.warning("Connect: Already connected.")
            self._print_to_cli("Already connected.")
            return True
        try:
            self.status_message = f"Connecting to {self.host}:{self.port}..."
            self.logger.info(f"Attempting to connect to {self.host}:{self.port}...")
            self._print_to_cli(self.status_message)
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_socket.settimeout(5.0) # don't hang forever
            self.client_socket.connect((self.host, self.port))
            self.client_socket.settimeout(None) # back to blocking for recv
            self.connected = True
            self.stop_receive.clear()
            self.receive_thread = threading.Thread(target=self.receive_messages_thread_func, daemon=True, name=f"{self.name}-ReceiveThread")
            self.receive_thread.start()
            self.status_message = "Connected. Please login/register."
            self.logger.info("Successfully connected to server.")
            self._print_to_cli("Successfully connected. Please login or register.")
            return True
        except Exception as e:
            self.logger.error(f"Connection failed: {e}", exc_info=False) # exc_info=false for console looks
            self.status_message = f"Connection failed: {e}"
            self._print_to_cli(f"Connection failed: {e}")
            if self.client_socket: self.client_socket.close()
            self.client_socket = None; self.connected = False
            return False

    def send_message(self, msg_dict):
        if not self.connected or not self.client_socket:
            self.logger.error("Send: Not connected.")
            self._print_to_cli("Error: Not connected. Cannot send message.")
            return False
        try:
            json_msg = json.dumps(msg_dict)
            applied_delay_ms = 0
            command_being_sent = msg_dict.get("command")

            # apply simulated delays if any
            if command_being_sent == "PING" and self.simulated_ping_delay_ms > 0:
                applied_delay_ms = self.simulated_ping_delay_ms
            elif command_being_sent == "SESSION_ANSWER" and self.simulated_answer_delay_ms > 0:
                applied_delay_ms = self.simulated_answer_delay_ms
            elif self.simulated_send_delay_ms > 0: # general delay if others not set
                applied_delay_ms = self.simulated_send_delay_ms
            if applied_delay_ms > 0:
                delay_sec = applied_delay_ms / 1000.0
                self.logger.debug(f"Applying SEND delay: {delay_sec:.3f}s before sending {command_being_sent}")
                time.sleep(delay_sec)

            self.client_socket.sendall(json_msg.encode('utf-8'))
            self.logger.debug(f"SENT to server: CMD={command_being_sent}, Data={str(msg_dict)[:150]}") # log a bit of data
            return True
        except Exception as e:
            self.logger.error(f"Send error for {msg_dict.get('command')}: {e}", exc_info=True)
            self._print_to_cli(f"Error sending message: {e}")
            self.disconnect(reason=f"Send error: {e}") # disconnect on send error
            return False

    def receive_messages_thread_func(self):
        # this runs in a separate thread to get messages from server
        buffer = ""; self.logger.debug("Receive thread started.")
        while self.connected and not self.stop_receive.is_set():
            try:
                self.client_socket.settimeout(1.0); data = self.client_socket.recv(4096) # timeout so it can check stop_receive
                if not data: self.logger.info("Server closed connection."); self.disconnect("Server closed"); break # server gone
                buffer += data.decode('utf-8', errors='ignore') # ignore weird chars for now
                while buffer: # process all full json messages in buffer
                    try: msg, index = json.JSONDecoder().raw_decode(buffer); buffer = buffer[index:].lstrip(); self.handle_server_message(msg)
                    except json.JSONDecodeError: self.logger.debug(f"Incomplete JSON, waiting for more. Buffer start: {buffer[:50]}"); break # not a full message yet
            except socket.timeout: continue # just loop again if timeout
            except ConnectionResetError: self.logger.warning("Connection reset by server."); self.disconnect("Connection reset"); break
            except OSError as e: # socket errors
                if self.stop_receive.is_set() or not self.connected : break # if we are stopping, just exit
                self.logger.error(f"Network OS Error in recv: {e}", exc_info=True); self.disconnect(f"OS Error: {e}"); break
            except Exception as e: # any other error
                if self.stop_receive.is_set() or not self.connected: break # if stopping, exit
                self.logger.error(f"Unexpected error in recv thread: {e}", exc_info=True); self.disconnect(f"Receive error: {e}"); break
        self.logger.debug("Receive thread ended.")

    def handle_server_message(self, msg):
        # this gets called by receive_thread when a full message comes in
        if os.environ.get("PYTEST_CURRENT_TEST"): # for pytest automation
            self.received_messages_log.append({"timestamp": time.perf_counter(), "message": msg, "client_name": self.name})
            if self.test_event_handler: # if test has a handler, call it
                try: self.test_event_handler(self.name, msg)
                except Exception as e_test_h: self.logger.warning(f"Error in test_event_handler: {e_test_h}")

        command = msg.get("command")
        self.logger.debug(f"RECV from server: CMD={command}, Data={str(msg)[:200]}") # log a bit more data

        cli_output = [] # messages for interactive cli display

        if command == "REGISTER_RESPONSE":
            self.status_message = f"Register: {msg.get('message')}"
            cli_output.append(f"Register Response: {msg.get('message')}")
            if msg.get('success'): self.logger.info(f"Registration: {msg.get('message')}")
            else: self.logger.warning(f"Registration failed: {msg.get('message')}")

        elif command == "LOGIN_RESPONSE":
            if msg.get("success"):
                self.username = msg.get("username")
                self.status_message = f"Logged in as {self.username}!"
                cli_output.append(f"Login Successful! Welcome {self.username}.")
                cli_output.append("Type 'sessions' or 'help'.")
                self.logger.info(f"Login successful as {self.username}.")
                self.send_message({"command": "LIST_SESSIONS"}) # auto get sessions after login
                if self.receive_thread and self.receive_thread.is_alive() and not os.environ.get("PYTEST_CURRENT_TEST"):
                 threading.Timer(1.0, self.send_ping_periodically).start() # start pinging
            else:
                self.status_message = f"Login Failed: {msg.get('message')}"
                cli_output.append(f"Login Failed: {msg.get('message')}")
                self.logger.warning(f"Login failed: {msg.get('message')}")

        elif command == "SESSION_LIST":
            sessions = msg.get("sessions", [])
            self.logger.info(f"Received SESSION_LIST with {len(sessions)} sessions.")
            cli_output.append("--- Available Sessions ---")
            self.known_sessions_cache.clear() # clear old cache
            if not sessions: cli_output.append("No sessions available. Type 'create' to make one.")
            else:
                for s_info in sessions:
                    status = "In Progress" if s_info.get("game_in_progress") else "Waiting"
                    full_id = s_info.get("session_id", "ERR_ID"); display_id = full_id[:8] # show short id
                    self.known_sessions_cache[display_id] = full_id; self.known_sessions_cache[full_id] = full_id # cache both for lookup
                    cli_output.append(f"  ID: {display_id} (Host: {s_info.get('host_username')}, Players: {s_info.get('num_players')}, Status: {status})")
            cli_output.extend(["-------------------------", "Use 'join <ID>' or 'create'."])

        elif command == "CREATE_SESSION_SUCCESS":
            new_sid = msg.get("session_id")
            if new_sid:
                self.current_session_id = new_sid; self.is_session_host = True
                self.status_message = f"Session {new_sid[:8]} created (Host)."
                cli_output.extend([f"Session created! ID: {new_sid[:8]} (Full: {new_sid})", "You are in session. Type 'startgame' or 'leave'."])
                self.logger.info(f"Session {new_sid} created. I am host.")
                self._pending_join_sid_cli = new_sid # server will send lobby update, this helps link it
            else:
                cli_output.append("Error: Server confirmed session creation but no session ID."); self.logger.error("CREATE_SESSION_SUCCESS missing session_id.")

        elif command == "JOIN_SESSION_SUCCESS": # server ack'd our join request, actual join confirmed by lobby_update
            sid = msg.get('session_id', 'Unknown')
            self.status_message = f"Join ack'd for {sid[:8]}."
            cli_output.append(f"Join request for {sid[:8]} ack'd. Waiting for lobby update...")
            self.logger.info(f"JOIN_SESSION_SUCCESS for {sid} received.")
            # self._pending_join_sid_cli should already be set from cli command

        elif command == "LEAVE_SESSION_SUCCESS":
            prev_sid = self.current_session_id
            self.status_message = msg.get("message", "Left session.")
            cli_output.append(f"Successfully left session: {msg.get('message')}")
            self.logger.info(f"Left session {prev_sid[:8] if prev_sid else ''}: {msg.get('message')}")
            self.current_session_id = None; self.is_session_host = False
            self.send_message({"command": "LIST_SESSIONS"}) # get fresh list after leaving

        elif command == "SESSION_LOBBY_UPDATE":
            sid = msg.get("session_id")
            self.logger.debug(f"SESSION_LOBBY_UPDATE for {sid[:8]}. Current: {self.current_session_id[:8] if self.current_session_id else 'N/A'}. Pending: {self._pending_join_sid_cli[:8] if self._pending_join_sid_cli else 'N/A'}")
            # if we were pending this session, this update confirms we are in
            if self.current_session_id is None and sid == self._pending_join_sid_cli:
                self.current_session_id = sid; self.logger.info(f"LOBBY_UPDATE confirmed entry to session {sid[:8]}."); self._pending_join_sid_cli = None
            if self.current_session_id == sid: # make sure it's for our current session
                self.status_message = msg.get("message", f"Session {sid[:8]} Updated")
                self.is_session_host = (self.username == msg.get("host_username")) # check if we are host
                cli_output.append(f"--- Session Update ({sid[:8]}) ---")
                cli_output.append(f"  Info: {msg.get('message')}")
                cli_output.append(f"  Host: {msg.get('host_username')} {'(You)' if self.is_session_host else ''}")
                cli_output.append(f"  Players ({msg.get('num_players')}):")
                for p in msg.get("players", []): cli_output.append(f"    - {p['username']} (Score: {p['score']})")
                if self.is_session_host and msg.get("can_host_start") and not msg.get("game_in_progress"):
                    cli_output.append("  As host, you can type 'startgame'.")
                    if os.environ.get("PYTEST_CURRENT_TEST"): self.client_ready_for_game_event.set() # signal test
                elif msg.get("game_in_progress"): cli_output.append("  Game is currently in progress.")
                cli_output.append("------------------------------------")
                self.logger.info(f"Processed lobby update for current session. Host: {self.is_session_host}, Players: {msg.get('num_players')}")
            # else: self.logger.debug(f"Lobby update for other session {sid[:8]} ignored.") # can be noisy

        elif command == "SESSION_GAME_STARTING":
            if msg.get("session_id") == self.current_session_id:
                self.status_message = "Game Starting!"; cli_output.append(f"\n!!! GAME STARTING in {self.current_session_id[:8]} !!!")
                self.logger.info(f"Game STARTING in session {self.current_session_id}.")

        elif command == "SESSION_PROBLEM":
            if msg.get("session_id") == self.current_session_id:
                reception_tp = time.perf_counter(); reception_tw = time.time() # record when we got it
                if os.environ.get("PYTEST_CURRENT_TEST"): self.latest_problem_data = msg.copy(); self.problem_received_event.set() # for tests
                target_utc = msg.get("target_display_utc",0); q_text = msg.get("question",""); r_n = msg.get("round_number"); t_q = msg.get("total_questions")
                self.logger.info(f"PROBLEM_RECV | PerfCnt:{reception_tp:.4f} | WallClk:{reception_tw:.3f} | SrvTgtUTC:{target_utc:.3f} | Rnd:{r_n}/{t_q} | Q:{q_text[:30]}...")
                self.status_message = f"Q {r_n}/{t_q}"; cli_output.append(f"\n--- Q {r_n}/{t_q} (TgtUTC:{target_utc:.2f}) ---");
                for ql in q_text.split('\n'): cli_output.append(f"  {ql}") # display question multi-line
                # ... (rest of cli output formatting)
                cli_output.extend(["-------------------------", "Type '= <your_answer>' to respond."])


        elif command == "SESSION_ROUND_RESULT": # critical for display
            if msg.get("session_id") == self.current_session_id:
                self.status_message = "Round Over"
                self.logger.info(f"ROUND_RESULT | Winner={msg.get('winner')} | CorrectAns={msg.get('correct_answer')} | AllAns={msg.get('all_answers')} | Scores={msg.get('scores')}")
                if os.environ.get("PYTEST_CURRENT_TEST"): self.latest_round_result = msg.copy() # for tests
                cli_output.append("\n--- ROUND RESULT ---")
                cli_output.append(f"  Correct Answer: {msg.get('correct_answer')}")
                if msg.get("winner"): cli_output.append(f"  Winner: {msg.get('winner')}")
                else: cli_output.append("  No winner this round.")
                cli_output.append("  Submitted Answers:")
                for uname, details in msg.get("all_answers", {}).items(): cli_output.append(f"    {uname}: '{details['answer']}' ({details['time']}, Correct: {details['correct']})")
                cli_output.append("  Current Scores:")
                for uname, score in msg.get("scores", {}).items(): cli_output.append(f"    {uname}: {score}")
                cli_output.append("--------------------")

        elif command == "SESSION_GAME_OVER": # critical for display
             if msg.get("session_id") == self.current_session_id:
                self.status_message = "Game Over!"
                self.logger.info(f"GAME_OVER | Message: {msg.get('message')} | Final Scores: {msg.get('scores')}")
                if os.environ.get("PYTEST_CURRENT_TEST"): self.latest_game_over = msg.copy() # for tests
                cli_output.append("\n--- GAME OVER ---"); cli_output.append(f"  {msg.get('message')}")
                cli_output.append("  Final Scores:")
                for uname, score in msg.get("scores", {}).items(): cli_output.append(f"    {uname}: {score}")
                cli_output.extend(["-----------------", "Game ended. Back in lobby view."])
                self.current_session_id = None; self.is_session_host = False # reset session state
                threading.Timer(0.5, lambda: self.send_message({"command": "LIST_SESSIONS"}) if self.connected else None).start() # get sessions again

        elif command == "PONG": # usually just for logs, not direct cli display
            rx_payload = msg.get("original_payload_timestamp") # server sends back our timestamp
            if self.ping_start_time is not None and self.active_ping_payload == rx_payload: # if it matches current ping
                self.last_rtt_ms = (time.time() - self.ping_start_time) * 1000 # calculate rtt
                self.status_message = f"PONG (RTT: {self.last_rtt_ms:.0f}ms)"
                self.logger.info(f"PONG recv. RTT: {self.last_rtt_ms:.2f}ms. Sending UPDATE_RTT.")
                self.ping_start_time = None; self.active_ping_payload = None # clear ping state
                self.send_message({"command": "UPDATE_RTT", "rtt_ms": self.last_rtt_ms}) # tell server our rtt
            else: self.logger.warning(f"PONG recv mismatch. RX: {rx_payload}, Active: {self.active_ping_payload}") # old pong or something

        elif command == "ERROR": # server sent an error
            self.status_message = f"SERVER ERROR: {msg.get('message')}"
            cli_output.append(f"SERVER ERROR: {msg.get('message')}")
            self.logger.error(f"Received SERVER ERROR: {msg.get('message')}")

        elif command == "SERVER_SHUTDOWN":
            self.status_message = "Server Shutdown!"
            cli_output.append(f"!!! SERVER SHUTDOWN: {msg.get('message')} !!!")
            self.logger.warning(f"Received SERVER_SHUTDOWN: {msg.get('message')}")
            self.disconnect("Server shutdown command")

        else: # unhandled commands
            if command: # only log if 'command' field exists, otherwise it's maybe bad json
                cli_output.append(f"(Unhandled server command: {command})")
                self.logger.warning(f"Received unhandled command: {command}, Data: {str(msg)[:100]}")

        if cli_output: self._print_to_cli(cli_output) # print all accumulated lines


    def send_ping_periodically(self):
        # sends a ping if not already waiting for one
        if not self.connected or self.stop_receive.is_set(): return # don't ping if not connected
        if self.ping_start_time is None: # only send if no active ping
            try:
                self.active_ping_payload = time.time(); self.ping_start_time = self.active_ping_payload # store time and payload
                if self.send_message({"command": "PING", "payload_timestamp": self.active_ping_payload}):
                    self.logger.debug("PING sent.")
            except Exception as e: self.logger.error(f"Error sending PING: {e}", exc_info=True)
        if self.connected and not self.stop_receive.is_set() and not os.environ.get("PYTEST_CURRENT_TEST"): # if still good and not in test
            delay = 10.0 if self.ping_start_time is not None else 5.0 # longer delay if waiting for pong, shorter if ready to send
            threading.Timer(delay, self.send_ping_periodically).start() # schedule next one

    def disconnect(self, reason="User action"):
        # clean way to stop client
        if not self.connected and not self.client_socket: return # already disconnected
        self.logger.info(f"Disconnecting ({reason})... Session: {self.current_session_id[:8] if self.current_session_id else 'None'}")
        self.connected = False; self.stop_receive.set() # signal threads to stop
        if self.client_socket:
            try: self.client_socket.shutdown(socket.SHUT_RDWR) # tell other side we're done
            except Exception: pass # might already be closed
            try: self.client_socket.close()
            except Exception: pass
        self.client_socket = None
        if self.receive_thread and self.receive_thread.is_alive() and threading.current_thread() != self.receive_thread: # if thread exists and it's not THIS thread
            self.receive_thread.join(timeout=1.0) # wait for it to finish
            if self.receive_thread.is_alive(): self.logger.warning("Receive thread did not stop cleanly on disconnect.")
        self.receive_thread = None
        # reset client state
        self.username = None; self.current_session_id = None; self.is_session_host = False; self.last_rtt_ms = None
        self.ping_start_time = None; self.active_ping_payload = None; self._pending_join_sid_cli = None
        self.known_sessions_cache.clear()
        self.status_message = f"Disconnected: {reason}"
        # pytest attributes reset
        if os.environ.get("PYTEST_CURRENT_TEST"):
            self.client_ready_for_game_event.clear(); self.problem_received_event.clear()
            self.latest_problem_data = None; self.latest_round_result = None; self.latest_game_over = None
        self.logger.info(f"Disconnected. Reason: {reason}.")
        self._print_to_cli(f"\n--- {self.status_message} ---") # show disconnect msg


    def run_cli(self):
        # main loop for user input
        if not os.environ.get("PYTEST_CURRENT_TEST"): # don't print this if running tests
            self.logger.info("Standard CLI Client starting. Type 'help' for commands.")
            print("Standard CLI Client. Type 'help' for commands.") # initial console message

        while not self.stop_receive.is_set(): # loop until told to stop
            try:
                if os.environ.get("PYTEST_CURRENT_TEST"): time.sleep(0.05); continue # if in test, just loop and sleep

                self.display_status_and_prompt() # show current status and prompt
                with self.input_lock: self.current_input_buffer = "" # clear buffer before input()
                try:
                    user_input = input().strip() # get user input
                    if self.stop_receive.is_set(): break # check again after blocking input()
                except EOFError: self.logger.info("EOF on input, treating as exit."); user_input = "exit"; # ctrl-d
                except KeyboardInterrupt: self.logger.info("Ctrl-C on input, treating as exit."); user_input = "exit"; # ctrl-c

                with self.input_lock: self.current_input_buffer = user_input # store what user typed for redraws

                if not user_input: continue # empty input, just loop
                self.logger.info(f"CLI Command: {user_input}")
                parts = user_input.split(" ", 2) # split into command and args
                cmd = parts[0].lower()

                if cmd == "exit" or cmd == "quit":
                    self.disconnect(reason="User exit command"); break # bye!

                elif cmd == "help":
                    self._print_to_cli([
                        "\nAvailable commands:",
                        "  connect <host:port>          - Connect (e.g., connect 127.0.0.1:4242)",
                        "  register <user> <pass>     - Register a new account.",
                        "  login <user> <pass>        - Login to an existing account.",
                        "  logout                       - Logout (disconnects).",
                        "  sessions                     - List available game sessions.",
                        "  create                       - Create a new game session.",
                        "  join <id>                    - Join a game session (use 8-char or full ID).",
                        "  leave                        - Leave the current game session.",
                        "  startgame                    - (Host only) Start game in current session.",
                        "  = <your_answer_text>         - Submit an answer to a problem.",
                        "  ping                         - Manually send a PING to measure RTT.",
                        "  disconnect                   - Disconnect from the server.",
                        "  exit / quit                  - Close the client.\n"
                    ])

                elif cmd == "connect":
                    if self.connected: self._print_to_cli("Already connected. Disconnect first."); continue
                    addr = parts[1] if len(parts) > 1 else f"{DEFAULT_SERVER_HOST}:{DEFAULT_SERVER_PORT}" # use default if not given
                    try: h, p = addr.split(':'); self.host = h; self.port = int(p); self.connect()
                    except ValueError: self._print_to_cli("Invalid address. Use host:port"); self.logger.warning("Invalid connect format.")

                elif cmd == "disconnect": self.disconnect(reason="User command")

                elif not self.connected: # commands below need connection
                    self._print_to_cli("Not connected. Please 'connect' first."); self.logger.warning("Cmd attempted while not connected.")
                    continue

                elif cmd == "register":
                    if len(parts) < 3: self._print_to_cli("Usage: register <username> <password>"); continue
                    self.send_message({"command": "REGISTER", "username": parts[1], "password": parts[2]})
                elif cmd == "login":
                    if len(parts) < 3: self._print_to_cli("Usage: login <username> <password>"); continue
                    self.send_message({"command": "LOGIN", "username": parts[1], "password": parts[2]})
                elif cmd == "logout":
                    self.logger.info("Logging out via CLI command..."); self.disconnect(reason="User logout command")

                elif not self.username: # commands below need login
                    self._print_to_cli("Please login first."); self.logger.warning("Cmd attempted while not logged in.")
                    continue

                elif cmd == "sessions": self.send_message({"command": "LIST_SESSIONS"})
                elif cmd == "create":
                    if self.current_session_id: self._print_to_cli(f"Already in session {self.current_session_id[:8]}. Leave first."); continue
                    self._pending_join_sid_cli = None; self.send_message({"command": "CREATE_SESSION"})
                elif cmd == "join":
                    if self.current_session_id: self._print_to_cli(f"Already in session {self.current_session_id[:8]}. Leave first."); continue
                    if len(parts) < 2: self._print_to_cli("Usage: join <session_id>"); continue
                    id_to_join = self.known_sessions_cache.get(parts[1], parts[1]) # resolve short id or use as is
                    self._pending_join_sid_cli = id_to_join # mark that we're trying to join this
                    self.logger.info(f"Attempting to join session via CLI: {id_to_join[:8]}...")
                    self.send_message({"command": "JOIN_SESSION", "session_id": id_to_join})
                elif cmd == "leave":
                    self.logger.info(f"Sending LEAVE_SESSION from CLI (current: {self.current_session_id[:8] if self.current_session_id else 'None'}).")
                    self.send_message({"command": "LEAVE_SESSION"})
                elif cmd == "startgame":
                    if not self.current_session_id: self._print_to_cli("Not in a session."); continue
                    if not self.is_session_host: self._print_to_cli("Only the session host can start the game."); continue
                    self.send_message({"command": "START_SESSION_GAME"})
                elif cmd == "=": # for answers
                    if not self.current_session_id: self._print_to_cli("Not in an active game session."); continue
                    answer = user_input[len(cmd):].lstrip() # get text after "="
                    if not answer: self._print_to_cli("Usage: = <your_answer>"); continue
                    self.send_message({"command": "SESSION_ANSWER", "answer": answer})
                elif cmd == "ping":
                    if self.ping_start_time is not None: self._print_to_cli("(PING already active or awaiting PONG)")
                    else: self.send_ping_periodically() # manually trigger a ping
                else:
                    self._print_to_cli(f"Unknown command: {cmd}. Type 'help'."); self.logger.warning(f"Unknown CLI command: {cmd}")

            except EOFError: self.logger.info("EOF on input, exiting CLI loop."); self.disconnect("EOF"); break # should be caught by inner try now
            except KeyboardInterrupt: self.logger.info("Keyboard interrupt, exiting CLI loop."); self.disconnect("Ctrl+C"); break # same
            except Exception as e: # any other error in cli loop
                self.logger.error(f"CLI loop unhandled error: {e}", exc_info=True)

        if self.connected and not self.stop_receive.is_set() : self.disconnect("CLI loop ended") # make sure disconnected if loop ends weirdly
        self.logger.info(f"{self.name} CLI Exited.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multiplayer Reaction Game Client")
    parser.add_argument("--name", default="User", help="Name for this client instance.")
    parser.add_argument("--host", default=DEFAULT_SERVER_HOST, help="Server host.")
    parser.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT, help="Server port.")
    parser.add_argument("--pingdelay", type=int, default=0, help="Simulated ONE-WAY send delay for PINGs in ms.") # one-way
    parser.add_argument("--ansdelay", type=int, default=0, help="Simulated ONE-WAY send delay for SESSION_ANSWER in ms.") # one-way
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG level logging for console and file.")
    args = parser.parse_args()

    log_level_to_set = logging.DEBUG if args.debug else logging.INFO
    root_logger = logging.getLogger('') # get the root logger
    for handler in list(root_logger.handlers): # iterate a copy to modify
        if isinstance(handler, logging.StreamHandler) and handler.stream == sys.stderr: # default basicConfig handler goes to stderr
            root_logger.removeHandler(handler) # remove default console handler from root to avoid double console logs

    # setup file handler for this specific instance
    instance_log_file = f"client_{args.name.replace(' ', '_')}_{os.getpid()}.log" # unique log file
    fh_instance = logging.FileHandler(instance_log_file, mode='w')
    fh_instance.setLevel(log_level_to_set) # file gets debug if --debug, else info
    formatter_instance = logging.Formatter('%(asctime)s.%(msecs)03d - %(levelname)s - %(name)s [%(threadName)s] - %(message)s', datefmt='%H:%M:%S')
    fh_instance.setFormatter(formatter_instance)
    root_logger.addHandler(fh_instance) # add our file handler to root

    if args.debug: # if debug, we want console output too
        # check if a console handler for stdout/stderr already exists on root, if not, add one
        if not any(isinstance(h, logging.StreamHandler) and h.stream in (sys.stdout, sys.stderr) for h in root_logger.handlers):
            ch_debug = logging.StreamHandler(sys.stdout) # new console handler to stdout
            ch_debug.setLevel(logging.DEBUG)
            ch_debug.setFormatter(formatter_instance) # use same rich format for console debug thing
            root_logger.addHandler(ch_debug)
        root_logger.setLevel(logging.DEBUG) # ensure root processes DEBUG messages for all handlers
    else: # if not debug, we might still want INFO on console if one was added (e.g. by pytest)
        if any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers): # if any console handler exists
            root_logger.setLevel(logging.INFO) # set root to INFO, so console shows INFO, file shows its own level


    module_logger.info(f"Client '{args.name}' starting. Host: {args.host}:{args.port}, PING delay: {args.pingdelay}ms, ANS delay: {args.ansdelay}ms. Log Level: {log_level_to_set}")

    cli = StandardCLIClient(args.host, args.port, name=args.name)
    cli.simulated_ping_delay_ms = args.pingdelay
    cli.simulated_answer_delay_ms = args.ansdelay
    cli.run_cli()