import pytest
import time
import threading
import os
import sys
import logging

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src'))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

test_logger = logging.getLogger("pytest_tests")

try:
    from Server import Server, UserDataStore
    from Client import StandardCLIClient
    IMPORTS_SUCCESSFUL = True
    test_logger.info("Main: Successfully imported Server, UserDataStore, and StandardCLIClient")
except ImportError as e:
    IMPORTS_SUCCESSFUL = False
    print(f"CRITICAL: Module imports failed: {e}. Check src/__init__.py & filenames (server.py, client.py).")
    pytest.exit(f"CRITICAL: Module imports failed: {e}", returncode=2)

TEST_SERVER_HOST = '127.0.0.1'
TEST_SERVER_PORT = 12360 # new port
TEST_DB_FILE = 'core_tests_v4_db.json'
VALID_TEST_PASSWORD = "password123"
USER_HOST = "host_user_core_v2"
USER_JOINER = "joiner_user_core_v2"
USER_SPOOFER = "spoofer_user_core_v2"

@pytest.fixture(scope="module")
def game_server():
    if not IMPORTS_SUCCESSFUL: pytest.skip("Skipping game_server fixture.")
    if os.path.exists(TEST_DB_FILE): os.remove(TEST_DB_FILE)
    server = Server(host=TEST_SERVER_HOST, port=TEST_SERVER_PORT)
    server.user_store = UserDataStore(TEST_DB_FILE)
    server.MIN_PLAYERS_FOR_SESSION_START = 2
    server_thread = threading.Thread(target=server.start, daemon=True, name="TestServerThread")
    server_thread.start(); time.sleep(0.6)
    yield server
    test_logger.info("game_server fixture: Shutting down server...")
    server.shutdown(); server_thread.join(timeout=2)
    if os.path.exists(TEST_DB_FILE): os.remove(TEST_DB_FILE)
    test_logger.info("game_server fixture: Server shutdown complete.")

@pytest.fixture
def core_test_client_factory():
    if not IMPORTS_SUCCESSFUL: pytest.skip("Skipping client factory.")
    clients = []
    original_env = os.environ.get("PYTEST_CURRENT_TEST")
    os.environ["PYTEST_CURRENT_TEST"] = "1"
    def _create_client(name, simulated_send_delay_ms=0):
        client = StandardCLIClient(TEST_SERVER_HOST, TEST_SERVER_PORT, name=name,
                                simulated_send_delay_ms=simulated_send_delay_ms)
        clients.append(client)
        assert client.connect(), f"Client {name} failed to connect"
        cli_thread = threading.Thread(target=client.run_cli, daemon=True, name=f"{name}-CLI")
        cli_thread.start(); client.cli_thread = cli_thread
        return client
    yield _create_client
    test_logger.debug(f"core_test_client_factory teardown: Cleaning up {len(clients)} clients...")
    for client in clients:
        if client.connected: client.disconnect(reason="Factory Cleanup")
        if hasattr(client, 'cli_thread') and client.cli_thread.is_alive():
            client.stop_receive.set(); client.cli_thread.join(timeout=1)
    if original_env is None: os.environ.pop("PYTEST_CURRENT_TEST", None)
    else: os.environ["PYTEST_CURRENT_TEST"] = original_env

def register_and_login_client(client: StandardCLIClient, username, password):
    client.clear_message_log()
    client.logger.debug(f"Helper: Attempting initial login for {username}...")
    time_sent_initial_login = time.perf_counter()
    client.send_message({"command": "LOGIN", "username": username, "password": password})
    login_res_initial = client.wait_for_message("LOGIN_RESPONSE", timeout=3, after_timestamp=time_sent_initial_login)

    if login_res_initial and login_res_initial.get("success"):
        client.logger.info(f"Helper: Initial login successful for {username}.")
        client.username = username
        # client auto sends list_sessions after login in handle_server_message
        return # successfully logged in

    client.logger.info(f"Helper: Initial login failed for {username} (Resp: {login_res_initial}). Attempting registration...")
    time_sent_register = time.perf_counter()
    client.send_message({"command": "REGISTER", "username": username, "password": password})
    reg_res = client.wait_for_message("REGISTER_RESPONSE", timeout=3, after_timestamp=time_sent_register)
    assert reg_res, f"Helper: No REGISTER_RESPONSE for {username}."
    assert reg_res.get("success"), f"Helper: Registration failed for {username}: {reg_res.get('message')}"
    client.logger.info(f"Helper: Registration successful for {username}.")

    client.logger.debug(f"Helper: Attempting login for {username} after registration...")
    time_sent_final_login = time.perf_counter()
    client.send_message({"command": "LOGIN", "username": username, "password": password})
    login_res_final = client.wait_for_message("LOGIN_RESPONSE", timeout=3, after_timestamp=time_sent_final_login)
    assert login_res_final, f"Helper: No LOGIN_RESPONSE for {username} after registration."
    assert login_res_final.get("success"), f"Helper: Login post-reg failed for {username}: {login_res_final.get('message')}"
    client.username = username
    client.logger.info(f"Helper: Login post-reg successful for {username}.")
    # client auto sends list_sessions after login here too.

def setup_player_for_game(client_factory_fn, name, username, password,
                        simulated_rtt_path_delay_ms=0,
                        session_to_join=None):
    client = client_factory_fn(name=name, simulated_send_delay_ms=simulated_rtt_path_delay_ms)
    register_and_login_client(client, username, password)
    test_logger.debug(f"SetupPlayer: {name} establishing RTT (simulated one-way send for PINGs: {simulated_rtt_path_delay_ms}ms)")

    # store timestamp *before* ping that will lead to update_rtt on server
    # the update_rtt is what the server will use for this client's rtt.
    time_before_ping_for_rtt_update = time.perf_counter()
    for i in range(1):
        client.clear_message_log()
        ping_ts_for_pong = time.perf_counter() # for waiting for this specific pong
        client.send_ping_periodically()
        pong_timeout = 3 + (simulated_rtt_path_delay_ms * 2 / 1000.0)
        pong = client.wait_for_message("PONG", timeout=pong_timeout, after_timestamp=ping_ts_for_pong)
        assert pong, f"SetupPlayer: {name} PONG timeout (delay={simulated_rtt_path_delay_ms}ms, timeout_val={pong_timeout}s)"
        # wait for client to process pong, calculate rtt, and send update_rtt.
        # also wait for server to process update_rtt. this sleep is important.
        time.sleep(0.5 + (simulated_rtt_path_delay_ms / 1000.0)) # generous sleep
    reported_rtt = client.last_rtt_ms
    test_logger.debug(f"SetupPlayer: {name} RTT sequence done. Client's last_rtt_ms: {reported_rtt if reported_rtt else 'N/A'}ms. Server should have received UPDATE_RTT.")

    if session_to_join: # joiner
        client.clear_message_log()
        time_sent_join_cmd = time.perf_counter() # timestamp before sending join_session
        client._pending_join_sid_cli = session_to_join
        client.send_message({"command": "JOIN_SESSION", "session_id": session_to_join})

        join_res = client.wait_for_message("JOIN_SESSION_SUCCESS", timeout=3, after_timestamp=time_sent_join_cmd)
        assert join_res and join_res.get("session_id") == session_to_join, f"SetupPlayer: {name} failed to get JOIN_SESSION_SUCCESS for {session_to_join[:8]}"
        client.current_session_id = session_to_join

        # wait for session_lobby_update (broadcast by server after adding player)
        # use the timestamp from *before* sending join_session
        lobby_update = client.wait_for_message("SESSION_LOBBY_UPDATE", timeout=3, after_timestamp=time_sent_join_cmd)
        assert lobby_update, f"SetupPlayer: {name} did not get own lobby update after joining session {session_to_join[:8]}. Last 5 msgs: {client.get_last_messages(5)}"
        assert lobby_update.get("session_id") == session_to_join, f"SetupPlayer: {name} lobby update for wrong session ID."
        player_names = [p.get('username') for p in lobby_update.get("players", [])]
        assert client.username in player_names, f"SetupPlayer: {name} not in own lobby update player list."
        test_logger.debug(f"SetupPlayer: {name} joined {session_to_join[:8]}, got lobby update (players: {lobby_update.get('num_players')}).")
    else: # host
        client.clear_message_log()
        time_sent_create_cmd = time.perf_counter() # timestamp before sending create_session
        client.send_message({"command": "CREATE_SESSION"})

        create_res = client.wait_for_message("CREATE_SESSION_SUCCESS", timeout=3, after_timestamp=time_sent_create_cmd)
        assert create_res and create_res.get("session_id"), f"SetupPlayer: {name} failed to create session"
        client.current_session_id = create_res.get("session_id")
        client.is_session_host = True
        client._pending_join_sid_cli = client.current_session_id

        # wait for session_lobby_update (broadcast by server after adding host as player)
        # use the timestamp from *before* sending create_session
        lobby_update = client.wait_for_message("SESSION_LOBBY_UPDATE", timeout=3, after_timestamp=time_sent_create_cmd)
        assert lobby_update and lobby_update.get("num_players") == 1, f"SetupPlayer: {name} incorrect lobby update (1p) after creating. Got: {lobby_update}"
        test_logger.debug(f"SetupPlayer: {name} created session {client.current_session_id[:8]}.")
    return client

# --- test functions ---
def test_server_runs(game_server): # should pass
    if not IMPORTS_SUCCESSFUL: pytest.fail("Module imports failed.")
    assert game_server is not None; test_logger.info("Pytest: test_server_runs passed.")

def test_user_registration_and_login(game_server, core_test_client_factory): # refined
    if not IMPORTS_SUCCESSFUL: pytest.fail("Module imports failed.")
    client1 = core_test_client_factory(name="RegLogClient")
    test_logger.info("Pytest: Running test_user_registration_and_login...")

    time_before_whole_login_sequence = time.perf_counter()
    register_and_login_client(client1, "user_reglog_v3", VALID_TEST_PASSWORD)

    # the list_sessions command is sent by client from within handle_server_message for login_response.
    # so, session_list response should arrive after the login sequence started.
    list_sess_res = client1.wait_for_message("SESSION_LIST", timeout=3, after_timestamp=time_before_whole_login_sequence)
    assert list_sess_res is not None, f"Client {client1.name} did not receive SESSION_LIST after login."
    test_logger.info(f"Pytest: {client1.name} received session list. Registration and Login test passed.")

def test_session_management(game_server, core_test_client_factory): # refined
    if not IMPORTS_SUCCESSFUL: pytest.fail("Module imports failed.")
    test_logger.info("Pytest: Running test_session_management...")
    host = setup_player_for_game(core_test_client_factory, "HostSM_v2", USER_HOST + "_sm_v2", VALID_TEST_PASSWORD)
    session_id = host.current_session_id
    assert session_id, "Host failed to create session/get session_id in setup."

    # timestamp for joiner's actions, used to check host's subsequent lobby update
    time_joiner_starts_joining = time.perf_counter()
    joiner = setup_player_for_game(core_test_client_factory, "JoinerSM_v2", USER_JOINER + "_sm_v2", VALID_TEST_PASSWORD, session_to_join=session_id)

    # host waits for lobby update triggered by joiner's successful join
    lobby_host_after_joiner = host.wait_for_message("SESSION_LOBBY_UPDATE", timeout=3, after_timestamp=time_joiner_starts_joining)
    assert lobby_host_after_joiner and lobby_host_after_joiner.get("num_players") == 2, f"HostSM lobby (2p) incorrect. Got: {lobby_host_after_joiner}"
    assert lobby_host_after_joiner.get("can_host_start"), "HostSM should be able to start game."

    # joiner should have received its own lobby update during its setup_player_for_game call.
    # we can re-check its last lobby message if needed, or trust the setup's asserts.
    # the current setup_player_for_game already asserts joiner gets a lobby update.
    test_logger.info(f"Pytest: {joiner.name} joined. Host {host.name} sees 2 players and can start.")

    time_joiner_sends_leave = time.perf_counter()
    joiner.send_message({"command": "LEAVE_SESSION"})
    leave_res = joiner.wait_for_message("LEAVE_SESSION_SUCCESS", timeout=3, after_timestamp=time_joiner_sends_leave)
    assert leave_res, f"JoinerSM failed to leave session."
    joiner.current_session_id = None

    lobby_host_after_leave = host.wait_for_message("SESSION_LOBBY_UPDATE", timeout=3, after_timestamp=time_joiner_sends_leave)
    assert lobby_host_after_leave and lobby_host_after_leave.get("num_players") == 1, f"HostSM lobby (1p) after leave incorrect. Got: {lobby_host_after_leave}"
    test_logger.info(f"Pytest: {joiner.name} left. {host.name} sees 1 player. Session management test passed.")

def test_ping_pong_rtt_update(game_server, core_test_client_factory): # should be mostly okay
    if not IMPORTS_SUCCESSFUL: pytest.fail("Module imports failed.")
    test_logger.info("Pytest: Running test_ping_pong_rtt_update...")
    client = core_test_client_factory(name="PingClient_v2")
    register_and_login_client(client, "pinguser_test_v3", VALID_TEST_PASSWORD)
    client.clear_message_log()
    ts_ping = time.perf_counter()
    client.send_ping_periodically()
    pong_msg = client.wait_for_message("PONG", timeout=3, after_timestamp=ts_ping)
    assert pong_msg, "Client did not receive PONG."
    assert client.last_rtt_ms is not None and client.last_rtt_ms > 0, "Client RTT not calculated."
    test_logger.info(f"Pytest: {client.name} received PONG, RTT: {client.last_rtt_ms:.0f}ms. Test passed.")

# in tests/test.py

def test_s3_anti_spoof_score_clamping(game_server, core_test_client_factory): # this is the one that failed
    if not IMPORTS_SUCCESSFUL: pytest.fail("Module imports failed.")
    test_logger.info("Pytest: Running test_s3_anti_spoof_score_clamping (Victim as Host)...")

    # victim joins first and establishes a fast rtt. victim creates the session.
    victim_rtt_path_delay_ms = 30
    victim = setup_player_for_game(core_test_client_factory, "VictimS3_Host", "victim_host_as", VALID_TEST_PASSWORD,
                                simulated_rtt_path_delay_ms=victim_rtt_path_delay_ms)
    session_id = victim.current_session_id
    assert session_id, "Victim (as initial host) failed to create session."
    # at this point, victim has received its own lobby update (1 player).

    # spoofer joins. initially, it will also report its actual (fast) rtt.
    spoofer_actual_rtt_path_delay_ms = 20

    # modification: capture timestamp before spoofer attempts to join
    time_before_spoofer_joins = time.perf_counter()

    spoofer = setup_player_for_game(core_test_client_factory, "SpooferS3_Joiner", USER_SPOOFER + "_as_vj", VALID_TEST_PASSWORD,
                                    simulated_rtt_path_delay_ms=spoofer_actual_rtt_path_delay_ms,
                                    session_to_join=session_id)

    # victim (current host) waits for lobby update confirming 2 players.
    # use the timestamp from before the spoofer sent its join_session command.
    lobby_victim_after_spoofer_join = victim.wait_for_message("SESSION_LOBBY_UPDATE", timeout=3, after_timestamp=time_before_spoofer_joins)
    assert lobby_victim_after_spoofer_join, f"Victim host did not get lobby update after spoofer joined. Last 5: {victim.get_last_messages(5)}"
    assert lobby_victim_after_spoofer_join.get("num_players") == 2, f"Victim host lobby update should show 2 players. Got: {lobby_victim_after_spoofer_join.get('num_players')}"
    assert lobby_victim_after_spoofer_join.get("can_host_start"), f"Victim host cannot start. Lobby: {lobby_victim_after_spoofer_join}"
    test_logger.info("Pytest AS: Victim (host) confirmed spoofer joined and can start game.")

    # now, spoofer sends its fake high rtt update.
    spoofed_rtt_on_spoofer = 5000
    spoofer.send_message({"command": "UPDATE_RTT", "rtt_ms": spoofed_rtt_on_spoofer})
    test_logger.info(f"Pytest AS: {spoofer.name} sent spoofed RTT of {spoofed_rtt_on_spoofer}ms.")
    time.sleep(0.3) # let server process spoofed rtt

    # victim (host) starts the game.
    # server will calculate problem_start_time. max_rtt will be spoofer's 5000ms.
    # so problem_start_time_utc will be t_snr_exec + 2.5s.
    spoofer.simulated_send_delay_ms = spoofer_actual_rtt_path_delay_ms # spoofer answers with its actual fast net
    victim.simulated_send_delay_ms = victim_rtt_path_delay_ms # victim answers with its normal net

    spoofer.problem_received_event.clear(); victim.problem_received_event.clear()
    spoofer.latest_problem_data = None; victim.latest_problem_data = None
    ts_start_game = time.perf_counter()
    victim.send_message({"command": "START_SESSION_GAME"}) # victim is host

    assert victim.wait_for_message("SESSION_GAME_STARTING", timeout=3, after_timestamp=ts_start_game) is not None
    assert spoofer.wait_for_message("SESSION_GAME_STARTING", timeout=3, after_timestamp=ts_start_game) is not None

    problem_timeout = 7.0 # server internal 2.5s + sync delay based on 5000ms rtt (2.5s)
    ts_problem_wait_start = time.perf_counter()
    assert spoofer.wait_for_message("SESSION_PROBLEM", timeout=problem_timeout, after_timestamp=ts_problem_wait_start) is not None
    assert victim.wait_for_message("SESSION_PROBLEM", timeout=problem_timeout, after_timestamp=ts_problem_wait_start) is not None

    problem_details_as = spoofer.latest_problem_data
    q_ans_as = "5"
    if problem_details_as and "lemon" in problem_details_as.get("question","").lower(): q_ans_as = "5"
    elif problem_details_as and "cookies" in problem_details_as.get("question","").lower(): q_ans_as = "5"
    elif problem_details_as and "burger" in problem_details_as.get("question","").lower(): q_ans_as = "150"

    spoofer_human_react_sec = 0.01
    victim_human_react_sec = 0.5

    # send answers
    threading.Timer(spoofer_human_react_sec, lambda: spoofer.send_message({"command": "SESSION_ANSWER", "answer": q_ans_as, "session_id": session_id})).start()
    threading.Timer(victim_human_react_sec, lambda: victim.send_message({"command": "SESSION_ANSWER", "answer": q_ans_as, "session_id": session_id})).start()

    # check results - either client can be used to wait for the broadcast
    result_msg = spoofer.wait_for_message("SESSION_ROUND_RESULT", timeout=5)
    assert victim.wait_for_message("SESSION_ROUND_RESULT", timeout=5) is not None # ensure victim also gets it
    assert result_msg is not None, "No round result for anti-spoof."

    spoofer_ans_info = result_msg["all_answers"].get(spoofer.username)
    victim_ans_info = result_msg["all_answers"].get(victim.username)
    assert spoofer_ans_info and victim_ans_info, "Answer info missing in round result."
    spoofer_time = float(spoofer_ans_info["time"][:-1]); victim_time = float(victim_ans_info["time"][:-1])
    test_logger.info(f"Pytest AS (Victim Host): Spoofer t:{spoofer_time:.4f}s, Victim t:{victim_time:.4f}s, Winner:{result_msg.get('winner')}")

    # this is the crucial assertion that was failing in your last output
    assert result_msg.get("winner") == spoofer.username, f"Spoofer should have won. Winner was {result_msg.get('winner')}"
    assert spoofer_time <= 0.001 + 1e-9, f"Spoofer score ({spoofer_time:.4f}) not clamped as expected."
    assert victim_time > 0.01, f"Victim score ({victim_time:.4f}) abnormal."
    test_logger.info("Pytest: Anti-spoofing score clamping test (Victim Host) passed.")