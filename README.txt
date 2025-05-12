Number Cruncher: Online Multithreaded Math Quiz Game 
====================================================
Author -> Nikeel Ramharakh (2433669)
GitHub Repo -> https://github.com/Nikeel03/NumberCruncher

Number Cruncher is a terminal-based client-server multiplayer math quiz game written in Python. Players connect to a server, can register/login, create or join game sessions and then compete in rounds by answering simple math-related questions as fast as possible. The game utilises multithreading to handle multiple clients concurrently.

Prerequisites
-------------

- Python 3.x (developed with 3.11, should work on most recent versions).
- No external Python libraries are required to run the game itself. All imports (`socket`, `json`, `threading`, etc.) are part of the Python standard library.
- For development and running tests, `pytest` is used:

    pip install pytest

All commands below should be run from the *root directory* of the project (the directory containing `src/`, `tests/` and this `README` file).

How to Run
----------

1. Start the Server

    The server needs to be running before any clients can connect.

    - Navigate to the *root directory* in your terminal.
    - Run the server script:

        python src/Server.py

    - The server will start listening for client connections. By default, it listens on 0.0.0.0:4242.
        - Log messages will appear in the console and be saved to `server.log`.
        - User data will be saved in `user_data.json`.

2. Start the Client(s)

    You can run multiple client instances to simulate multiple players. Each client needs its own terminal window.

    - Open a new terminal window.
    - Navigate to the *root directory*.
    - Run the client script:

        Basic client:
            python src/Client.py

        Client with a specific name:
            python src/Client.py --name Player1

        Client connecting to a specific server host/port:
            python src/Client.py --host <server_ip_address> --port <server_port_number>

        Client with simulated network delays (for testing):
            python src/Client.py --name PlayerWithLag --pingdelay 100 --ansdelay 200
            (pingdelay and ansdelay are one-way send delays in milliseconds)

        Client with debug logging to console:
            python src/Client.py --debug

    - Once the client starts, connect to the server:

        connect 127.0.0.1:4242

      (Assuming the server is running on the same machine with default port 4242).

        - Type `help` in the client terminal to see available commands.
        - Common first steps after connecting:
            - register <username> <password>
            - login <username> <password>

        - Log messages will appear in the console (if `--debug` is used) and be saved to a unique log file.

Client Commands
---------------

| Command              | Description                                                                 |
|----------------------|-----------------------------------------------------------------------------|
| sessions             | List available game sessions.                                               |
| create               | Create a new game session (you become the host).                            |
| join <session_id>    | Join an existing session (use the 8-character short ID or the full ID).     |
| leave                | Leave the current game session.                                             |
| startgame            | (Host only) Start the game in the current session.                          |
| = <answer>           | Submit an answer during a game round.                                       |
| logout               | Log out from the server (also disconnects).                                 |
| disconnect           | Disconnect from the server.                                                 |
| exit or quit         | Close the client application.                                               |
| Ctrl + C             | Keyboard Interrupt.                                                         |

Running Tests
-------------

If you have `pytest` installed and your tests are in `tests/Test.py`:

1. Navigate to the *root directory*.
2. Run:

    pytest tests/Test.py -s

The `-s` flag is optional, but useful as it shows output (like print statements and log messages) directly to the console during the test run.
