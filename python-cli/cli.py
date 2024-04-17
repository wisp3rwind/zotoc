__all__ = [
    "ask_yn",
    "html_color_block",
    "select",
]


# TUI libraries:
# - https://github.com/prompt-toolkit/python-prompt-toolkit
# - https://github.com/Textualize/textual
# - https://github.com/rothgar/awesome-tuis?tab=readme-ov-file#libraries
# - https://github.com/jquast/blessed
# - https://github.com/Textualize/rich


def select(msg, options):
    if len(options) == 0:
        raise ValueError("Empty options")

    if len(options) == 1:
        idx, opt = 0, options[0]
        print(f"Choosing {idx: 3d}: {opt}")
    else:
        for i, opt in enumerate(options):
            print(f"{i: 3d}: {opt}")

        while True:
            idx = int(input(f"{msg}: "))
            if idx >= 0 and idx < len(options):
                break

        opt = options[idx]

    return idx, opt


def html_color_block(color, size=5):
    """
    """
    c = color.removeprefix("#")
    r = int(c[0:2], 16)
    g = int(c[2:4], 16)
    b = int(c[4:6], 16)
    set_fg = f"\033[38;2;{r};{g};{b}m"
    reset = "\033[0m"
    return set_fg + "█" * size + reset


def ask_yn(msg):
    while True:
        answer = input(msg + " [y/n]")
        answer = answer.strip().lower()
        if answer in ["y", "yes"]:
            return True
        elif answer in ["n", "no"]:
            return False
        print("Please enter y[es] or n[o]!")
