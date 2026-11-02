import sys
if sys.version_info < (3, 10):
    sys.exit("Python 3.10+ required")
import pr_tools
if __name__ == "__main__":
    try:
        sys.exit(pr_tools.main(sys.argv[1:]))
    except (RuntimeError, ValueError, OSError, KeyboardInterrupt, EOFError) as error:
        print("Error: " + str(error), file=sys.stderr)
        sys.exit(1)
