/* tiocsti-inject — feed stdin bytes into a pseudo-terminal via TIOCSTI.
 *
 * Installed setuid-root so it can inject into a terminal that is not the
 * caller's controlling tty (the kernel requires CAP_SYS_ADMIN for that, even
 * when dev.tty.legacy_tiocsti=1). To contain that privilege it refuses any
 * target that is not a /dev/pts/* device owned by the *real* (invoking) user,
 * so a user can only ever inject into their own terminals.
 *
 * Usage:  tiocsti-inject /dev/pts/N      (bytes to inject are read from stdin)
 *
 * Build:  gcc -O2 -Wall -o tiocsti-inject tiocsti-inject.c
 * Install: sudo install -o root -g root -m 4755 tiocsti-inject /usr/local/bin/
 */
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <termios.h>

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s /dev/pts/N\n", argv[0]);
        return 2;
    }
    const char *path = argv[1];

    if (strncmp(path, "/dev/pts/", 9) != 0) {
        fprintf(stderr, "tiocsti-inject: refusing non-pts target: %s\n", path);
        return 3;
    }

    struct stat st;
    if (stat(path, &st) != 0) {
        perror("tiocsti-inject: stat");
        return 4;
    }
    if (st.st_uid != getuid()) {
        fprintf(stderr, "tiocsti-inject: refusing: %s is not owned by you\n", path);
        return 5;
    }

    int fd = open(path, O_WRONLY | O_NOCTTY);
    if (fd < 0) {
        perror("tiocsti-inject: open");
        return 6;
    }

    int c;
    while ((c = getchar()) != EOF) {
        char ch = (char)c;
        if (ioctl(fd, TIOCSTI, &ch) < 0) {
            perror("tiocsti-inject: ioctl TIOCSTI");
            close(fd);
            return 7;
        }
    }
    close(fd);
    return 0;
}
