# Number Cruncher: Online Multithreaded Math Quiz Game

Number Cruncher is a command-line based client-server multiplayer math quiz game written in Python. Players connect to a server, can register/login, create or join game sessions, and then compete in rounds by answering simple math-related questions as fast as possible. The game utilizes multithreading to handle multiple clients concurrently.

## Prerequisites

*   **Python 3.x** (developed with 3.7+, should work on most recent versions).
*   No external Python libraries are required to *run* the game itself. All imports (`socket`, `json`, `threading`, etc.) are part of the Python standard library.
*   For *development and running tests*, `pytest` is used:
    ```bash
    pip install pytest
    ```
All commands below should be run from the **root directory** of the project (the directory containing `src/`, `tests/`, and this `README.md`).

## How to Run

### 1. Start the Server

The server needs to be running before any clients can connect.

1.  Navigate to the **root directory** in your terminal.
2.  Run the server script:
    ```bash
    python src/Server.py
    ```
3.  The server will start listening for client connections. By default, it listens on `0.0.0.0:4242`.
    *   Log messages will appear in the console and be saved to `server.log` (in the directory where `Server.py` is run, i.e., `src/`).
    *   User data will be saved in `user_data.json` (in `src/`).

### 2. Start the Client(s)

You can run multiple client instances to simulate multiple players. Each client needs its own terminal window.

1.  Open a new terminal window.
2.  Navigate to the **root directory**.
3.  Run the client script:
    *   **Basic client:**
        ```bash
        python src/Client.py
        ```
    *   **Client with a specific name:**
        ```bash
        python src/Client.py --name Player1
        ```
    *   **Client connecting to a specific server host/port (if not default):**
        ```bash
        python src/Client.py --host <server_ip_address> --port <server_port_number>
        ```
    *   **Client with simulated network delays (for testing):**
        ```bash
        python src/Client.py --name PlayerWithLag --pingdelay 100 --ansdelay 200
        ```
        (`pingdelay` and `ansdelay` are one-way send delays in milliseconds)
    *   **Client with debug logging to console:**
        ```bash
        python src/Client.py --debug
        ```

4.  Once the client starts, you'll need to connect to the server. The first command you'll likely use in the client terminal is:
    ```
    connect 127.0.0.1:4242
    ```
    (Assuming the server is running on the same machine with default port `4242`).
    *   Type `help` in the client terminal to see available commands.
    *   Common first steps *after connecting*:
        *   `register <username> <password>`
        *   `login <username> <password>`
    *   Log messages will appear in the console (if `--debug` is used) and be saved to a unique `client_<name>_<pid>.log` file (in the directory where `Client.py` is run, i.e., `src/`).

### Client Commands

Once connected and logged in, you can use commands like:

*   `sessions`: List available game sessions.
*   `create`: Create a new game session (you become the host).
*   `join <session_id>`: Join an existing session (use the 8-character short ID or the full ID).
*   `leave`: Leave the current game session.
*   `startgame`: (Host only) Start the game in the current session.
*   `= <answer>`: Submit an answer during a game round.
*   `logout`: Log out from the server (also disconnects).
*   `disconnect`: Disconnect from the server.
*   `exit` or `quit`: Close the client application.

## Running Tests

If you have `pytest` installed and your tests are in `tests/Test.py`:

1.  Navigate to the **root directory**.
2.  Run:
    ```bash
    pytest tests/Test.py -s
    ```
    The `-s` flag is optional but useful as it shows output (like print statements and log messages) directly to the console during the test run.
3.  This will execute the tests defined in `Test.py`. Test-specific files like `core_tests_v4_db.json` might be created and deleted in the `tests/` directory during the test run.

## Important Notes

*   Running the scripts from the root directory as specified (`python src/Server.py` and `python src/Client.py`) ensures that generated files like logs and the user database are created within the `src/` directory. If you run them from within `src/` directly (e.g., `cd src; python Server.py`), these files will appear in `src/` as well. The key is consistency.
*   The test setup script (`tests/Test.py` in your example) modifies `sys.path` to include the `src` directory. This is important for the tests to find the `Server` and `Client` modules.
