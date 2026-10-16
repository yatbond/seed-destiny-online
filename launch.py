import sys

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        from seedrooms.diagnostics import run
        raise SystemExit(run(sys.argv[2]))
    from seedrooms.desktop import main
    main()
