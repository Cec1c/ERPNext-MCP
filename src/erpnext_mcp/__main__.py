"""Module entrypoint that avoids locking the Windows console-script launcher."""

from .server import main

if __name__ == "__main__":
    main()
