/* freestanding の最小の文字列・メモリの関数（FreeRTOS とコンパイラの生成する呼び出しのため）。 */
#include <string.h>

void *memcpy(void *dst, const void *src, size_t n) {
    unsigned char *d = dst;
    const unsigned char *s = src;
    while (n--) {
        *d++ = *s++;
    }
    return dst;
}

void *memset(void *dst, int value, size_t n) {
    unsigned char *d = dst;
    while (n--) {
        *d++ = (unsigned char) value;
    }
    return dst;
}

size_t strlen(const char *s) {
    size_t n = 0;
    while (s[n] != '\0') {
        n++;
    }
    return n;
}

char *strcpy(char *dst, const char *src) {
    char *d = dst;
    while ((*d++ = *src++) != '\0') {
    }
    return dst;
}
