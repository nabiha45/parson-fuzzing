#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

#include "../parson/parson.h"

enum { ACCEPTED = 0, REJECTED = 1, HARNESS_ERROR = 2 };

/* Preserve every input byte, then add the terminator required by Parson's API.
 * No grammar checks or UTF-8 repair: the parser remains the test target. */
static char *read_input(FILE *stream) {
    size_t length = 0;
    size_t capacity = 4096;
    char *buffer = (char *)malloc(capacity);
    if (buffer == NULL) {
        fputs("HARNESS_ERROR: cannot allocate input buffer\n", stderr);
        return NULL;
    }
    for (;;) {
        size_t count;
        if (length == capacity - 1) {
            char *grown;
            if (capacity > SIZE_MAX / 2) {
                fputs("HARNESS_ERROR: input too large to buffer\n", stderr);
                free(buffer);
                return NULL;
            }
            grown = (char *)realloc(buffer, capacity * 2);
            if (grown == NULL) {
                fputs("HARNESS_ERROR: cannot grow input buffer\n", stderr);
                free(buffer);
                return NULL;
            }
            buffer = grown;
            capacity *= 2;
        }
        count = fread(buffer + length, 1, capacity - length - 1, stream);
        length += count;
        if (ferror(stream)) {
            fputs("HARNESS_ERROR: input read failed\n", stderr);
            free(buffer);
            return NULL;
        }
        if (feof(stream)) break;
    }
    buffer[length] = '\0';
    return buffer;
}

int main(int argc, char **argv) {
    FILE *stream = stdin;
    char *input;
    JSON_Value *value;
    int status;

    if (argc > 2) {
        fprintf(stderr, "Usage: %s [input-file|-] (no argument reads stdin)\n", argv[0]);
        return HARNESS_ERROR;
    }
    if (argc == 2 && strcmp(argv[1], "-") != 0) {
        stream = fopen(argv[1], "rb");
        if (stream == NULL) {
            perror("HARNESS_ERROR: cannot open input file");
            return HARNESS_ERROR;
        }
    }
#ifdef _WIN32
    else if (_setmode(_fileno(stdin), _O_BINARY) == -1) {
        perror("HARNESS_ERROR: cannot set stdin to binary mode");
        return HARNESS_ERROR;
    }
#endif
    input = read_input(stream);
    if (stream != stdin && fclose(stream) != 0) {
        perror("HARNESS_ERROR: cannot close input file");
        free(input);
        return HARNESS_ERROR;
    }
    if (input == NULL) return HARNESS_ERROR;

    /* A non-NULL value means Parson accepted a prefix, not necessarily the
     * complete document. Embedded NUL bytes terminate this C-string API. */
    value = json_parse_string(input);
    status = value == NULL ? REJECTED : ACCEPTED;
    json_value_free(value);
    free(input);
    /* Print only after cleanup: sanitizer failures during cleanup must not
     * produce a misleading successful verdict. Do not catch crash signals. */
    puts(status == ACCEPTED ? "ACCEPTED" : "REJECTED");
    return status;
}
