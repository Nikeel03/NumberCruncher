import socket
import threading
import json
import time
import random
import hashlib
import uuid
import logging
import sys

# logging stuff
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s.%(msecs)03d - %(levelname)s - %(name)s - [%(threadName)s] - %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler("server.log", mode='w'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("server") # using 'logger' here because it's the var name

HOST = '0.0.0.0'
PORT = 4242
DB_FILE = 'user_data.json'
MIN_PLAYERS_FOR_SESSION_START = 2

# password hashing and checking
def hash_password(password,salt=None):
    if salt is None: salt = uuid.uuid4().hex.encode('utf-8')
    else: salt = salt.encode('utf-8')
    hashed_password = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt,100000)
    return salt.decode('utf-8'),hashed_password.hex()
def verify_password(stored_salt,stored_password_hash,provided_password):
    _, hashed_password = hash_password(provided_password,stored_salt)
    return hashed_password == stored_password_hash

class UserDataStore:
    def __init__(self, filepath):
        self.filepath = filepath
        self.logger = logging.getLogger(f"server.UserDataStore") # var name
        self.users = self._load_users()
        self.lock = threading.RLock()
        self.logger.debug(f"UserDataStore initialized for file: {filepath}")

    def _load_users(self):
        try:
            with open(self.filepath, 'r') as f: data = json.load(f)
            for username in list(data.keys()): # quick cleanup if old 'friends' key exists
                if "friends" in data[username]: del data[username]["friends"]
            self.logger.info(f"User data loaded from {self.filepath}. {len(data)} users found.")
            return data
        except FileNotFoundError:
            self.logger.info(f"User data file {self.filepath} not found. Starting with empty users.")
            return {}
        except json.JSONDecodeError:
            self.logger.error(f"Corrupted user data file: {self.filepath}.", exc_info=True)
            return {}

    def _save_users(self):
        with self.lock:
            try:
                with open(self.filepath, 'w') as f: json.dump(self.users, f, indent=4)
                self.logger.info(f"User data saved to {self.filepath}. Total users: {len(self.users)}.")
            except IOError as e:
                self.logger.error(f"Could not save user data to {self.filepath}: {e}", exc_info=True)

    def register_user(self, username, password):
        with self.lock:
            if username in self.users:
                self.logger.warning(f"Reg failed (user exists): {username}")
                return False, "Username already exists."
            if not username or len(username) < 3:
                self.logger.warning(f"Reg failed (user short): {username}")
                return False, "Username too short (min 3 chars)."
            if not password or len(password) < 6:
                self.logger.warning(f"Reg failed (pass short): {username}")
                return False, "Password too short (min 6 chars)."
            salt, hashed_pwd = hash_password(password)
            self.users[username] = {"salt": salt, "password_hash":hashed_pwd}
            self._save_users()
            self.logger.info(f"User '{username}' registered.")
            return True, "Registration successful."

    def login_user(self,username,password):
        with self.lock:
            user_data = self.users.get(username)
            if not user_data:
                self.logger.warning(f"Login failed (no user): {username}")
                return False, "Username not found."
            if verify_password(user_data["salt"], user_data["password_hash"], password):
                self.logger.info(f"User '{username}' logged in.")
                return True, "Login successful."
            else:
                self.logger.warning(f"Login failed (bad pass): {username}")
                return False, "Invalid password."

class GameSession:
    def __init__(self,session_id,host_client_id,host_username,server_ref):
        self.session_id = session_id
        self.host_client_id = host_client_id
        self.host_username = host_username
        self.server = server_ref # ref to main server
        self.players = {}
        self.game_state = { "game_in_progress": False, "current_problem": None, "answers_received": {},
                            "problem_start_time": 0, "waiting_for_next_round": False,
                            "asked_questions_indices": set(), "game_over_sent": False, "current_round_timers": [] }
        self.lock = threading.RLock()
        self.game_problems = [
            {"question":"           | 🍋🍬🍭🍬🍋 |\n               | 🍭🍭🍬🍋🍬 |\n                       | 🍬🍋🍭🍭🍋 |\n                       |_____JAR____|\n                       How many 🍋 lemon sweets are there?", "answer": "5"},
            {"question":"           | 🔒🍪 🍪 🔒🍪 |\n              | 🍪 🔒🍪 🍪 🍪 |\n                       | 🔒🍪 🍪 🔒🍪 |\n                       |_____JAR____|\n                       How many cookies are secure?", "answer": "5"},
            {"question":"🍔 A burger server makes 5 burgers per minute. If 3 servers work for 10 minutes, how many burgers are served?", "answer": "150"},
        ]
        self.session_logger = logging.getLogger(f"server.Session.{self.session_id[:8]}") # var name
        self.session_logger.info(f"Created by {host_username} ({host_client_id})")

        # add host to players, need their socket from server's client list
        host_client_data = self.server.clients.get(host_client_id)
        if host_client_data and host_client_data.get('socket'):
            self.add_player(host_client_id, host_username, host_client_data['socket'])
        else:
            self.session_logger.error(f"Host client {host_client_id} data or socket not found when creating session.")

    def update_player_rtt(self,client_id,rtt_ms):
        with self.lock:
            player = self.players.get(client_id)
            if player:
                player["estimated_rtt"] = rtt_ms
                self.session_logger.info(f"Updated RTT for {player.get('username','Unknown')} ({client_id}): {rtt_ms}ms")
            else:
                self.session_logger.warning(f"Attempted to update RTT for non-existent player {client_id} in session.")

    def add_player(self, client_id, username, client_socket):
        with self.lock:
            if client_id in self.players:
                self.session_logger.warning(f"Player {username} ({client_id}) attempted to join session again.")
                return False, "Already in this session."
            if self.game_state["game_in_progress"] and not self.game_state["waiting_for_next_round"]:
                self.session_logger.warning(f"Player {username} ({client_id}) tried to join mid-active-round.")
                return False, "Game actively in progress, cannot join mid-question."

            self.players[client_id] = { "username": username, "socket": client_socket, "score": 0,
                                        "join_time": time.time(), "estimated_rtt": 200 } # default rtt
            self.session_logger.info(f"Player {username} ({client_id}) joined. Total players: {len(self.players)}.")
            self.broadcast_session_update()
            return True, "Joined session."

    def remove_player(self, client_id):
        player_left_username = None; was_host = False; new_host_username_designated = None
        with self.lock:
            if client_id in self.players:
                player_info = self.players.pop(client_id)
                player_left_username = player_info["username"]
                self.session_logger.info(f"Player {player_left_username} ({client_id}) left session.")

                if client_id == self.host_client_id:
                    was_host = True; self.host_client_id = None; self.host_username = None
                    if self.players:
                        # new host = earliest joiner
                        new_host_id, new_host_data = sorted(self.players.items(), key=lambda item: item[1]["join_time"])[0]
                        self.host_client_id = new_host_id
                        self.host_username = new_host_data["username"]
                        new_host_username_designated = self.host_username
                        self.session_logger.info(f"New host designated: {self.host_username} ({self.host_client_id}).")
                    else:
                        self.session_logger.info("Session became hostless and empty after host left.")

                # if player left mid-round, ditch their answer
                if (self.game_state["game_in_progress"] or self.game_state["waiting_for_next_round"]) and \
                    client_id in self.game_state["answers_received"]:
                    del self.game_state["answers_received"][client_id]
                    self.session_logger.debug(f"Removed answer from {player_left_username} who left mid-round.")

                # see if round/game needs ending
                if self.game_state["game_in_progress"] or self.game_state["waiting_for_next_round"]:
                    if not self.players or (self.players and len(self.game_state["answers_received"]) >= len(self.players)):
                        self.session_logger.info("Player left, triggering round/game state check.")
                        self._clear_pending_round_timers() # clear before new timer
                        threading.Timer(0.2, self.evaluate_round_or_end_game_if_needed).start() # wait a sec
        
        if player_left_username: # only broadcast if someone actually left
            self.broadcast_session_update()
        return player_left_username, was_host, new_host_username_designated


    def evaluate_round_or_end_game_if_needed(self):
        with self.lock:
            if not self.players and (self.game_state["game_in_progress"] or self.game_state["waiting_for_next_round"]):
                self.session_logger.info("All players left. Ending game and cleaning session.")
                self.game_state["game_in_progress"] = False; self.game_state["waiting_for_next_round"] = False
                self.game_state["current_problem"] = None; self._clear_pending_round_timers()
                # server will remove empty session via check_empty_session
                self.server.check_empty_session(self.session_id)
                return

            if self.game_state["current_problem"] and self.players and \
                len(self.game_state["answers_received"]) >= len(self.players) and \
                self.game_state["game_in_progress"]: # make sure we only eval once
                self.session_logger.debug("All answers received or conditions met for evaluation.")
                self.evaluate_round()

    def _clear_pending_round_timers(self):
        with self.lock:
            num_timers = len(self.game_state.get("current_round_timers", []))
            if num_timers > 0: self.session_logger.debug(f"Clearing {num_timers} pending round timers.")
            for timer in self.game_state.get("current_round_timers", []):
                try: timer.cancel()
                except Exception as e: self.session_logger.warning(f"Error cancelling a timer: {e}", exc_info=False)
            self.game_state["current_round_timers"] = []

    def broadcast_to_session_players(self, message_dict, exclude_client_id=None):
        message_json = json.dumps(message_dict); message_bytes = message_json.encode('utf-8')
        player_sockets_to_send = []
        cmd_for_log = message_dict.get('command','UNKNOWN_BROADCAST')
        with self.lock:
            if not self.players: self.session_logger.debug(f"No players to broadcast {cmd_for_log}."); return
            for cid, p_info in list(self.players.items()):
                if exclude_client_id and cid == exclude_client_id: continue
                player_sockets_to_send.append(p_info["socket"])

        self.session_logger.debug(f"Broadcasting {cmd_for_log} to {len(player_sockets_to_send)} players.")
        for sock in player_sockets_to_send:
            try: sock.sendall(message_bytes)
            except Exception as e:
                self.session_logger.error(f"Broadcast error for {cmd_for_log} to a client: {e}", exc_info=False)

    def broadcast_session_update(self):
        with self.lock:
            if self.game_state["game_in_progress"] and self.game_state["current_problem"] and \
                not self.game_state["waiting_for_next_round"]:
                self.session_logger.debug("Suppressing lobby update: question active.")
                return
            # ... (making the update message) ...
            num_players = len(self.players)
            host_username = self.host_username if self.host_client_id else "N/A (Session Hostless)"
            message = f"Session {self.session_id[:8]}. Host: {host_username}. Players: {num_players}."
            can_host_start = (self.host_client_id is not None and \
                            num_players >= self.server.MIN_PLAYERS_FOR_SESSION_START and \
                            not self.game_state["game_in_progress"] and \
                            not self.game_state["waiting_for_next_round"])
            if not self.host_client_id and num_players > 0:
                message = f"Session {self.session_id[:8]} is waiting for a host. Players: {num_players}."
            elif num_players < self.server.MIN_PLAYERS_FOR_SESSION_START:
                message += f" Waiting for {self.server.MIN_PLAYERS_FOR_SESSION_START - num_players} more."
            elif can_host_start:
                message += " Ready for host to start."
            player_list = [{"username": p["username"], "score": p["score"]} for p in self.players.values()]
            update_msg = {
                "command": "SESSION_LOBBY_UPDATE", "session_id": self.session_id, "message": message,
                "host_username": host_username, "num_players": num_players,
                "min_players_for_start": self.server.MIN_PLAYERS_FOR_SESSION_START,
                "can_host_start": can_host_start, "players": player_list,
                "game_in_progress": self.game_state["game_in_progress"] or self.game_state["waiting_for_next_round"]
            }
        self.broadcast_to_session_players(update_msg)

    def start_game(self, requesting_client_id):
        with self.lock:
            if requesting_client_id != self.host_client_id:
                self.session_logger.warning(f"Non-host {requesting_client_id} tried to start game.")
                return False, "Only the host can start the game."
            if len(self.players) < self.server.MIN_PLAYERS_FOR_SESSION_START:
                self.session_logger.warning(f"Host {self.host_username} tried to start game with {len(self.players)} players (need {self.server.MIN_PLAYERS_FOR_SESSION_START}).")
                return False, f"Need at least {self.server.MIN_PLAYERS_FOR_SESSION_START} players."
            if self.game_state["game_in_progress"] or self.game_state["waiting_for_next_round"]:
                self.session_logger.warning(f"Host {self.host_username} tried to start game when already active/starting.")
                return False, "Game is already active or starting."

            self._clear_pending_round_timers()
            self.game_state["asked_questions_indices"].clear(); self.game_state["game_over_sent"] = False
            for p_id in self.players: self.players[p_id]["score"] = 0

            self.game_state["waiting_for_next_round"] = True; self.game_state["game_in_progress"] = False
            self.session_logger.info(f"Game starting sequence initiated by host {self.host_username}.")
            self.broadcast_to_session_players({"command": "SESSION_GAME_STARTING", "session_id": self.session_id, "message": "Game is starting!"})
            self.broadcast_session_update()

        start_delay_timer = threading.Timer(2.5, self.start_new_round)
        with self.lock: self.game_state["current_round_timers"].append(start_delay_timer)
        start_delay_timer.start()
        return True, "Game starting sequence initiated."

    def send_problem_to_player(self, client_id, problem_msg_dict):
        send_perf_time = time.perf_counter(); send_wall_time = time.time()
        player_data_username = "Unknown"; can_send = False
        with self.lock:
            player_data = self.players.get(client_id)
            if player_data:
                player_data_username = player_data['username']
                if self.game_state["game_in_progress"] and self.game_state["current_problem"] and \
                   self.game_state["current_problem"]["question"] == problem_msg_dict.get("question"):
                    can_send = True
                else:
                    self.session_logger.warning(f"send_problem: Aborting send to {player_data_username} ({client_id}). Game state/problem mismatch.")
            else:
                self.session_logger.warning(f"send_problem: Player {client_id} (was {player_data_username}) not found when trying to send problem.")

        if can_send:
            self.session_logger.info(
                f"SENDING_PROBLEM to {player_data_username} ({client_id}) | "
                f"PerfCounter: {send_perf_time:.4f} | WallClock: {send_wall_time:.3f} | "
                f"TargetUTC: {problem_msg_dict.get('target_display_utc'):.3f}"
            )
            self.server.send_to_client(client_id, problem_msg_dict)

    def start_new_round(self):
        self.session_logger.info("Attempting to start new round.")
        with self.lock:
            self._clear_pending_round_timers()
            if self.game_state["game_in_progress"] and not self.game_state["waiting_for_next_round"]:
                self.session_logger.warning("Cannot start new round, one is already active and not completed.")
                return
            self.game_state["waiting_for_next_round"] = False
            if not self.players:
                self.session_logger.info("No players for new round. Game ends for session.")
                self.game_state["game_in_progress"] = False; self.broadcast_session_update()
                self.server.check_empty_session(self.session_id); return
            available_indices = [i for i in range(len(self.game_problems)) if i not in self.game_state["asked_questions_indices"]]
            if not available_indices:
                self.session_logger.info("All questions asked. Game Over.")
                if not self.game_state["game_over_sent"]:
                    scores = {p["username"]: p["score"] for p in self.players.values()}
                    self.broadcast_to_session_players({"command": "SESSION_GAME_OVER", "session_id": self.session_id, "message":"All questions done!", "scores":scores})
                    self.game_state["game_over_sent"] = True
                self.game_state["game_in_progress"] = False; self.broadcast_session_update(); return

            self.game_state["game_in_progress"] = True
            idx = random.choice(available_indices); self.game_state["asked_questions_indices"].add(idx)
            self.game_state["current_problem"] = self.game_problems[idx]; self.game_state["answers_received"] = {}

            max_rtt_ms = 0; player_rtts = {}
            if not self.players: # double check after state changes
                self.session_logger.warning("No players at RTT calculation. Aborting round."); self.game_state["game_in_progress"] = False; self.broadcast_session_update(); return
            for cid, p_info in self.players.items():
                rtt = p_info.get("estimated_rtt", 300);
                if rtt is None or not isinstance(rtt, (int, float)) or rtt <=0 : rtt = 300 # sanity check rtt
                player_rtts[cid] = rtt;
                if rtt > max_rtt_ms: max_rtt_ms = rtt
            max_one_way_latency_sec = (max_rtt_ms / 2.0) / 1000.0
            self.game_state["problem_start_time"] = time.time() + max_one_way_latency_sec # this is when everyone should see it

            self.session_logger.info(f"Max RTT: {max_rtt_ms}ms. Max 1-way sync latency: {max_one_way_latency_sec*1000:.2f}ms.")
            self.session_logger.info(f"Server time.time() now: {time.time():.3f}. Target problem display UTC (problem_start_time): {self.game_state['problem_start_time']:.3f}")
            problem_msg_base = {"command": "SESSION_PROBLEM", "session_id": self.session_id, "question": self.game_state["current_problem"]["question"],
                                "round_number": len(self.game_state["asked_questions_indices"]), "total_questions": len(self.game_problems),
                                "target_display_utc": self.game_state["problem_start_time"]}
            if not self.players: self.session_logger.critical("No players when about to schedule timers. Aborting round."); self.game_state["game_in_progress"] = False; self.broadcast_session_update(); return
            for cid, p_info in list(self.players.items()): # list() for safe iteration if player leaves
                client_rtt_ms = player_rtts.get(cid, max_rtt_ms) # use player's rtt or max if somehow missing
                client_one_way_latency_sec = (client_rtt_ms / 2.0) / 1000.0
                delay_send_by_sec = max_one_way_latency_sec - client_one_way_latency_sec # send earlier to faster clients
                if delay_send_by_sec < 0: delay_send_by_sec = 0 # can't be negative
                self.session_logger.info(f"Scheduling problem for {p_info['username']} ({cid}) with dispatch_delay: {delay_send_by_sec:.3f}s (Player RTT: {client_rtt_ms}ms). Target display UTC: {problem_msg_base['target_display_utc']:.3f}")
                timer = threading.Timer(delay_send_by_sec, self.send_problem_to_player, args=[cid, problem_msg_base.copy()])
                self.game_state["current_round_timers"].append(timer); timer.start()
            self.session_logger.info(f"All problem dispatches scheduled for round {len(self.game_state['asked_questions_indices'])}.")
            self.broadcast_session_update() # tell clients game is in progress (hides start button etc)

    def handle_answer(self, client_id, answer_text):
        answer_arrival_perf = time.perf_counter(); answer_arrival_wall = time.time()
        with self.lock:
            player_info = self.players.get(client_id)
            if not player_info: self.session_logger.warning(f"Answer from {client_id}: player not found."); return
            if not self.game_state["game_in_progress"] or not self.game_state["current_problem"]:
                self.session_logger.warning(f"Answer from {player_info['username']}: game not active or no problem."); return
            if client_id in self.game_state["answers_received"]:
                self.session_logger.warning(f"Duplicate answer from {player_info['username']}."); return
            time_taken = answer_arrival_wall - self.game_state["problem_start_time"]
            if time_taken < 0: # if they answered "before" the problem was "officially" displayed due to clock sync or aggressive client
                self.session_logger.warning(f"Player {player_info['username']} answered with negative time_taken ({time_taken:.3f}s). Clamping to 0.001s. ArrivalWall: {answer_arrival_wall:.3f}, ProblemStartUTC: {self.game_state['problem_start_time']:.3f}")
                time_taken = 0.001 # clamp to a tiny positive
            self.game_state["answers_received"][client_id] = {"answer": answer_text, "time_taken": time_taken, "arrival_perf_counter": answer_arrival_perf}
            self.session_logger.info(f"Player {player_info['username']} answered. Time taken (rel to target): {time_taken:.3f}s.")
            if len(self.game_state["answers_received"]) >= len(self.players): # all active players answered
                self._clear_pending_round_timers(); self.evaluate_round()

    def evaluate_round(self):
        eval_start_perf = time.perf_counter(); self.session_logger.info(f"Evaluating round (State: InProgress={self.game_state['game_in_progress']}).")
        round_result_msg = None
        with self.lock:
            if not self.game_state["current_problem"]: # check if already evaluated or weird state
                self.session_logger.warning("Evaluate_round called but no current problem (already evaluated or race).")
                # ... (if no players, etc.)
                return
            self.game_state["game_in_progress"] = False # round answering phase is over
            self._clear_pending_round_timers() # cancel any lingering timers (e.g. problem timeout if we had one)
            correct_ans = self.game_state["current_problem"]["answer"]
            fastest_time = float('inf'); winner_cid = None; results = {}
            for cid, ans_info in self.game_state["answers_received"].items():
                player = self.players.get(cid);
                if not player: self.session_logger.warning(f"Player {cid} who answered is no longer in session during eval."); continue
                is_correct = (str(ans_info["answer"]).strip().lower() == str(correct_ans).strip().lower())
                results[player["username"]] = {"answer": ans_info["answer"], "time": f"{ans_info['time_taken']:.3f}s", "correct": is_correct}
                if is_correct and ans_info["time_taken"] < fastest_time:
                    fastest_time = ans_info["time_taken"]; winner_cid = cid
            winner_username = None
            if winner_cid and winner_cid in self.players: # make sure winner is still here
                self.players[winner_cid]["score"] +=1; winner_username = self.players[winner_cid]["username"]
            scores = {p["username"]:p["score"] for p in self.players.values()}
            round_result_msg = {"command": "SESSION_ROUND_RESULT", "session_id": self.session_id, "correct_answer": correct_ans,
                                "winner": winner_username, "all_answers": results, "scores": scores}
            self.game_state["current_problem"] = None; self.game_state["waiting_for_next_round"] = True # set up for next round or game over
        if round_result_msg:
            self.broadcast_to_session_players(round_result_msg)
            self.session_logger.info(f"Round result sent. Winner: {winner_username if winner_username else 'None'}.")
            next_round_delay_timer = threading.Timer(5.0, self.start_new_round) # delay before next round starts
            with self.lock: self.game_state["current_round_timers"].append(next_round_delay_timer)
            next_round_delay_timer.start()
        else: # should not happen if logic is correct
            self.session_logger.error("No round result message to send, this should not happen if evaluate_round is called correctly.")
            if self.players: self.start_new_round() # try to recover if players exist
            else: # no players, session might be dead
                self.server.check_empty_session(self.session_id)


class Server:
    def __init__(self, host, port):
        self.host = host; self.port = port; self.server_socket = None
        self.clients = {}; self.sessions = {}; self.user_store = UserDataStore(DB_FILE)
        self.main_lock = threading.RLock(); self.username_to_client_id = {}
        self.MIN_PLAYERS_FOR_SESSION_START = MIN_PLAYERS_FOR_SESSION_START
        logger.info(f"Server instance initialized for {host}:{port}.")

    def start(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.server_socket.bind((self.host, self.port)); self.server_socket.listen(5)
            logger.info(f"Listening on {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to bind or listen on {self.host}:{self.port}: {e}", exc_info=True)
            if self.server_socket: self.server_socket.close()
            self.server_socket = None; return # can't start

        logger.info("Server accept loop started.")
        try:
            while self.server_socket: # keep accepting if socket is good
                client_socket, address = self.server_socket.accept()
                client_id = f"{address[0]}:{address[1]}" # simple id from ip:port
                logger.info(f"Accepted connection from {address}, client_id: {client_id}")
                with self.main_lock:
                    if client_id in self.clients: # unlikely, but handle if same client connects fast
                        logger.warning(f"Client ID {client_id} ({address}) already exists. Closing old socket.")
                        try: self.clients[client_id]['socket'].close()
                        except: pass # old socket might be dead anyway
                    self.clients[client_id] = {"socket": client_socket, "address": address, "username": None, "current_session_id": None}
                thread = threading.Thread(target=self.handle_client, args=(client_id,), name=f"ClientThread-{client_id.replace(':', '-')}")
                thread.daemon = True; thread.start()
        except OSError as e: # e.g. socket closed during accept
            if self.server_socket is None: logger.info("Server accept loop ending: socket closed by shutdown.")
            else: logger.error(f"OSError in accept loop: {e}", exc_info=False) # no need for full trace for common accept errors
        except KeyboardInterrupt: logger.info("KeyboardInterrupt: Shutting down server...")
        except Exception as e: logger.critical(f"Unexpected critical error in accept loop: {e}", exc_info=True)
        finally:
            if self.server_socket is not None: self.shutdown() # make sure we shutdown if loop breaks badly
            logger.info("Server accept loop terminated.")

    def shutdown(self):
        logger.info("Initiating server shutdown sequence...")
        if self.server_socket:
            try: self.server_socket.close(); logger.info("Server listening socket closed.")
            except Exception as e: logger.error(f"Error closing server listening socket: {e}", exc_info=True)
        self.server_socket = None # signal that we're down
        logger.info("Clearing active game sessions...")
        with self.main_lock:
            for sess_id in list(self.sessions.keys()): # list() for safe iteration while modifying
                session = self.sessions.pop(sess_id, None)
                if session: logger.debug(f"Clearing timers for session {sess_id}"); session._clear_pending_round_timers()
            self.sessions.clear()
        logger.info("Closing all client connections...")
        with self.main_lock:
            for client_id, client_data in list(self.clients.items()):
                if client_data and client_data.get('socket'):
                    try: client_data['socket'].sendall(json.dumps({"command":"SERVER_SHUTDOWN", "message":"Server is shutting down."}).encode('utf-8'))
                    except Exception: pass # client might be gone already
                    try: client_data['socket'].close()
                    except Exception as e_sock: logger.warning(f"Error closing socket for {client_id}: {e_sock}", exc_info=False)
            self.clients.clear(); self.username_to_client_id.clear()
        logger.info("Server shutdown sequence complete.")

    def send_to_client(self, client_id, message_dict):
        sock_to_send = None; client_data = None; client_desc = client_id
        with self.main_lock: client_data = self.clients.get(client_id)
        if client_data and client_data["socket"]:
            sock_to_send = client_data["socket"]
            if client_data.get("username"): client_desc = f"{client_data.get('username')}({client_id})"
        else:
            logger.warning(f"send_to_client: Client {client_id} not found or no socket for command {message_dict.get('command','N/A')}.")
            return False
        try:
            sock_to_send.sendall(json.dumps(message_dict).encode('utf-8'))
            logger.debug(f"SENT to {client_desc}: CMD={message_dict.get('command')}, Data={str(message_dict)[:100]}") # short log for data
            return True
        except (ConnectionResetError, BrokenPipeError, OSError) as e: # common network issues
            logger.warning(f"Network error sending to {client_desc} cmd {message_dict.get('command','N/A')}: {e}", exc_info=False)
            # self.cleanup_client(client_id) # might do this from handle_client when recv fails
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending to {client_desc}: {e}", exc_info=True)
            return False

    def cleanup_client(self, client_id):
        logger.info(f"Cleaning up client {client_id}...")
        username_to_remove = None; current_session_id_of_client = None; game_session_instance = None
        client_desc = client_id # default desc
        with self.main_lock:
            client_data = self.clients.pop(client_id, None)
            if not client_data: logger.warning(f"Client {client_id} already cleaned up or never existed."); return # already gone
            if client_data.get("username"): client_desc = f"{client_data.get('username')}({client_id})" # better desc if known
            try: client_data["socket"].close()
            except Exception: pass # don't care if close fails, it's dead to us
            username_to_remove = client_data.get("username")
            current_session_id_of_client = client_data.get("current_session_id")
            if username_to_remove and self.username_to_client_id.get(username_to_remove) == client_id: # if this client_id was the one for that username
                self.username_to_client_id.pop(username_to_remove, None)
            if current_session_id_of_client and current_session_id_of_client in self.sessions:
                game_session_instance = self.sessions.get(current_session_id_of_client) # get session to remove player from
        if game_session_instance:
            logger.info(f"Removing {client_desc} from session {game_session_instance.session_id}")
            game_session_instance.remove_player(client_id) # tell session to remove
            self.check_empty_session(game_session_instance.session_id) # see if session is now empty
        logger.info(f"Client {client_desc} fully cleaned up.")


    def check_empty_session(self, session_id):
        session_to_check = None; is_session_empty = False
        with self.main_lock: session_to_check = self.sessions.get(session_id) # get session safely
        if session_to_check:
            with session_to_check.lock: is_session_empty = not session_to_check.players # check players under session lock
            if is_session_empty:
                with self.main_lock: # remove from server's main dict under main lock
                    removed_session = self.sessions.pop(session_id, None)
                    if removed_session:
                        removed_session._clear_pending_round_timers() # stop any timers in the session
                        logger.info(f"Game session {session_id} was empty and has been removed.")


    def handle_client(self,client_id):
        client_socket = None; client_desc_for_log = client_id
        with self.main_lock:
            client_data = self.clients.get(client_id)
            if not client_data: logger.error(f"handle_client: No data for {client_id}"); return # should not happen
            client_socket = client_data["socket"]
            if client_data.get("username"): client_desc_for_log = f"{client_data.get('username')}({client_id})"
        logger.debug(f"Handler started for client {client_desc_for_log}.")
        buffer = "" # for accumulating partial messages
        try:
            while self.server_socket and client_id in self.clients: # loop while server is up and client connected
                try: data = client_socket.recv(4096) # read some data
                except ConnectionResetError: logger.warning(f"Client {client_desc_for_log} reset connection."); break
                except OSError as e: logger.warning(f"Client {client_desc_for_log} OS error in recv: {e}"); break # e.g. socket closed
                if not data: logger.info(f"Client {client_desc_for_log} disconnected (empty data)."); break # clean disconnect
                buffer += data.decode('utf-8',errors='ignore') # add to buffer, ignore weird chars for now (debug)
                while buffer: # process all full json messages in buffer
                    try:
                        msg, index = json.JSONDecoder().raw_decode(buffer) # try to decode one json obj
                        buffer = buffer[index:].lstrip() # remove processed part and leading whitespace
                        with self.main_lock: current_data = self.clients.get(client_id, {}).copy() # get fresh client data
                        if not current_data: # client got removed mid-process
                            logger.warning(f"Client {client_desc_for_log} data disappeared. Disconnecting."); return
                        self.process_command(client_id, current_data, msg)
                    except json.JSONDecodeError: logger.debug(f"Incomplete JSON from {client_desc_for_log}. Buffer: {buffer[:50]}..."); break # not a full message yet, wait for more
                    except Exception as e_proc: # error in process_command
                        logger.error(f"Error processing cmd from {client_desc_for_log}: {e_proc} - Msg: {str(msg)[:200]}", exc_info=True)
                        try: self.send_to_client(client_id, {"command":"ERROR", "message":"Server error processing request."})
                        except: pass # client might be gone
                        buffer = "" # clear buffer to avoid reprocessing bad data
        except Exception as e: # any other unhandled error in this thread
            logger.error(f"Unhandled exception in handle_client for {client_desc_for_log}: {e}", exc_info=True)
        finally:
            self.cleanup_client(client_id) # always cleanup
            logger.debug(f"Handler terminated for client {client_desc_for_log}.")


    def process_command(self, client_id,client_data_copy,msg):
        command = msg.get("command"); username = client_data_copy.get("username")
        current_session_id = client_data_copy.get("current_session_id"); game_session=None
        client_log_tag = f"<{username or client_id}>" # for cleaner logs

        if command not in ["UPDATE_RTT", "PING"]: # don't spam logs with these
            logger.info(f"{client_log_tag} CMD_RECV: {command}, Data: {str(msg)[:100]}")
        else:
            logger.debug(f"{client_log_tag} CMD_RECV: {command} (RTT/Ping data not shown for brevity)")

        if command == "REGISTER":
            uname = msg.get("username"); passwd = msg.get("password")
            success, message = self.user_store.register_user(uname, passwd)
            self.send_to_client(client_id, {"command": "REGISTER_RESPONSE", "success": success, "message": message})

        elif command == "LOGIN":
            if username: # already logged in
                self.send_to_client(client_id, {"command": "LOGIN_RESPONSE", "success": False, "message": "Already logged in.", "username": username})
                return
            uname = msg.get("username"); passwd = msg.get("password")
            with self.main_lock: # check if user is active elsewhere
                if uname in self.username_to_client_id and self.username_to_client_id[uname] != client_id:
                    if self.username_to_client_id[uname] in self.clients: # if the *other* client is still connected
                        self.send_to_client(client_id, {"command": "LOGIN_RESPONSE", "success": False, "message": f"User '{uname}' already logged in elsewhere."})
                        return
                    else: self.username_to_client_id.pop(uname, None) # old, stale entry, clear it
            success, message = self.user_store.login_user(uname, passwd)
            if success:
                with self.main_lock: # update client's state on server
                    s_client_data = self.clients.get(client_id) # re-fetch, could have disconnected
                    if s_client_data: s_client_data["username"] = uname; self.username_to_client_id[uname] = client_id
                    else: logger.warning(f"Client {client_id} disconnected before username set on login."); return # too late
                self.send_to_client(client_id, {"command": "LOGIN_RESPONSE", "success": True, "message": message, "username": uname})
            else: self.send_to_client(client_id, {"command": "LOGIN_RESPONSE", "success": False, "message": message})

        elif not username: # all commands below require login
            logger.warning(f"{client_log_tag} Attempted command '{command}' without login.")
            self.send_to_client(client_id, {"command": "ERROR", "message": "Not logged in."})
            return

        # commands requiring login start here
        elif command == "UPDATE_RTT":
            rtt_ms = msg.get("rtt_ms")
            if isinstance(rtt_ms, (int, float)) and rtt_ms>0: # basic validation
                if current_session_id: # if in a session, tell the session
                    with self.main_lock: game_session = self.sessions.get(current_session_id)
                    if game_session: game_session.update_player_rtt(client_id, rtt_ms)
            # no response needed for update_rtt usually
            return

        elif command == "LIST_SESSIONS":
            with self.main_lock: # need lock for iterating sessions
                session_list = []
                for sid, sess in self.sessions.items():
                    with sess.lock: # need session's lock for its details
                        session_list.append({ "session_id": sid, "host_username": sess.host_username,
                                            "num_players": len(sess.players),
                                            "game_in_progress": sess.game_state["game_in_progress"] or sess.game_state["waiting_for_next_round"]})
            self.send_to_client(client_id, {"command": "SESSION_LIST", "sessions": session_list})

        elif command == "CREATE_SESSION":
            if current_session_id: # can't create if already in one
                self.send_to_client(client_id, {"command":"ERROR", "message":"Already in a session. Leave first."}); return
            session_id_new = str(uuid.uuid4()) # new unique id
            new_session = GameSession(session_id_new, client_id, username, self) # create it
            with self.main_lock: # add to server's list
                self.sessions[session_id_new] = new_session
                s_client_data = self.clients.get(client_id) # re-fetch
                if s_client_data: s_client_data["current_session_id"]=session_id_new
                else: # client gone
                    logger.warning(f"Client {client_id} disconnected before session ID set on create.");
                    self.sessions.pop(session_id_new, None); new_session._clear_pending_round_timers(); return
            self.send_to_client(client_id, {"command": "CREATE_SESSION_SUCCESS", "session_id": session_id_new, "message": "Session created."})
            # session will send its own first lobby update

        elif command == "JOIN_SESSION":
            if current_session_id: # can't join if already in one
                self.send_to_client(client_id, {"command":"ERROR", "message":"Already in a session. Leave first."}); return
            target_session_id = msg.get("session_id")
            with self.main_lock: session_to_join = self.sessions.get(target_session_id) # find session
            if session_to_join:
                client_sock = None # need client's socket for the session
                with self.main_lock: c_data = self.clients.get(client_id); client_sock = c_data["socket"] if c_data else None
                if not client_sock: self.send_to_client(client_id, {"command":"ERROR", "message": "Client data error."}); return # client gone
                success, message = session_to_join.add_player(client_id, username, client_sock) # try to add
                if success:
                    with self.main_lock: # update client's state on server
                        s_client_data = self.clients.get(client_id) # re-fetch
                        if s_client_data: s_client_data["current_session_id"] = target_session_id
                        else: logger.warning(f"Client {client_id} disconnected before session ID set on join."); session_to_join.remove_player(client_id); return # client gone, remove from session
                    self.send_to_client(client_id, {"command":"JOIN_SESSION_SUCCESS", "session_id": target_session_id, "message": message})
                    # session will broadcast lobby update
                else: self.send_to_client(client_id, {"command":"ERROR", "message": f"Failed to join: {message}"})
            else: self.send_to_client(client_id, {"command":"ERROR", "message":"Session not found."})

        elif command == "LEAVE_SESSION":
            if not current_session_id: self.send_to_client(client_id, {"command":"ERROR", "message":"Not in a session."}); return
            with self.main_lock: game_session = self.sessions.get(current_session_id) # find current session
            if not game_session: # session might have died
                self.send_to_client(client_id, {"command":"ERROR", "message":"Session no longer exists."})
                with self.main_lock: s_client_data = self.clients.get(client_id); s_client_data["current_session_id"] = None if s_client_data else None # clear their session id
                return
            game_session.remove_player(client_id) # tell session to remove
            with self.main_lock: s_client_data = self.clients.get(client_id); s_client_data["current_session_id"] = None if s_client_data else None # clear their session id
            self.send_to_client(client_id, {"command":"LEAVE_SESSION_SUCCESS", "message":"You have left the session."})
            self.check_empty_session(current_session_id) # see if session is now empty

        elif command == "START_SESSION_GAME":
            if not current_session_id: self.send_to_client(client_id, {"command":"ERROR", "message":"Not in a session."}); return
            with self.main_lock: game_session = self.sessions.get(current_session_id)
            if not game_session: self.send_to_client(client_id, {"command":"ERROR", "message":"Session not found."}); return
            success, message = game_session.start_game(client_id) # try to start
            if not success: self.send_to_client(client_id, {"command":"ERROR", "message":message})
            # session sends game_starting if successful

        elif command == "SESSION_ANSWER":
            if not current_session_id: self.send_to_client(client_id, {"command":"ERROR", "message":"Not in a session."}); return
            with self.main_lock: game_session = self.sessions.get(current_session_id)
            if not game_session: self.send_to_client(client_id, {"command":"ERROR", "message":"Session not found."}); return
            answer = msg.get("answer"); game_session.handle_answer(client_id, answer) # pass to session
            # session handles response (round result)

        elif command == "PING":
            client_payload_ts = msg.get("payload_timestamp") # client sends its timestamp
            if client_payload_ts is not None: # make sure we got it
                self.send_to_client(client_id, {"command": "PONG", "original_payload_timestamp": client_payload_ts}) # send it back
            else: # bad ping
                logger.warning(f"{client_log_tag} Sent PING without payload_timestamp.")
                self.send_to_client(client_id, {"command":"ERROR", "message":"Invalid PING."})
        else: # unknown command
            logger.warning(f"{client_log_tag} Received unknown command: {command}")
            self.send_to_client(client_id, {"command":"ERROR", "message":f"Unknown command: {command}"})


if __name__ == "__main__":
    logger.info(f"Server starting up on {HOST}:{PORT}...")
    server = Server(HOST, PORT)
    try: server.start()
    except Exception as e_main: # catch anything that makes start() crash badly
        logger.critical(f"Server main execution failed: {e_main}", exc_info=True)
    finally:logger.info("Server process has finished or is shutting down.")