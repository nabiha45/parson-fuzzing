#include <stdio.h>
#include <string.h>
#include <time.h>
#include "../parson/parson.h"

static unsigned int accepted, rejected;

/* Print a readable preview, escaping raw bytes and control characters. */
static void run_test(const char *id, const char *description, const char *input)
{
    size_t i, length = strlen(input);
    JSON_Value *value;
    clock_t start, end;
    printf("%s %s\n  [", id, description);
    if (length == 0)
        printf("<empty input>");
    for (i = 0; i < length && i < 80; i++)
    {
        unsigned char c = (unsigned char)input[i];
        if (c == '\n')
            printf("\\n");
        else if (c == '\r')
            printf("\\r");
        else if (c == '\t')
            printf("\\t");
        else if (c < 32 || c >= 127)
            printf("\\x%02X", (unsigned int)c);
        else
            putchar(c);
    }
    if (length > 80)
        printf("... <preview; %lu bytes>", (unsigned long)length);
    printf("] => ");
    fflush(stdout); /* Identify the running case even if parsing crashes. */
    start = clock();
    value = json_parse_string(input);
    end = clock();
    if (value)
        accepted++;
    else
        rejected++;
    printf("%s", value ? "ACCEPTED" : "REJECTED");
    if (start != (clock_t)-1 && end != (clock_t)-1)
    {
        printf(" (parse CPU time %.3f ms)", 1000.0 * (double)(end - start) / CLOCKS_PER_SEC);
    }
    putchar('\n');
    fflush(stdout);
    if (value == NULL)
    {
        fprintf(stderr, "[stderr] Test %s (%s): json_parse_string returned NULL; no detailed parser error is available.\n",
                id, description);
        fflush(stderr);
    }
    json_value_free(value);
}

int main(void)
{
    char long_input[12009];
    char nested_input[4101];
    char wide_input[12000];
    char label[80];
    size_t i, used;
    const size_t depths[] = {10, 2048, 2049, 2050};
    const char *depth_ids[] = {"4.1", "4.2", "4.3", "4.extra"};

    puts("Parson batch tests: actual parser verdicts (not strict-JSON conformance verdicts)");
    puts("Large inputs are constructed in C; only their printed previews are shortened.\n");
    run_test("2.15", "Unterminated string", "{\"a\":\"abc");
    run_test("2.14", "Incomplete object with escaped NUL key", "{\"\\u0000\"");
    run_test("2.16", "Empty string", "{\"a\":\"\"}");
    memcpy(long_input, "{\"a\":\"", 6);
    memset(long_input + 6, 'a', 12000);
    memcpy(long_input + 12006, "\"}", 3);
    run_test("2.17", "Long string: 12,000 characters", long_input);
    run_test("UTF8", "Previous test: invalid UTF-8 bytes C3 28", "{\"a\":\""
                                                                 "\xC3\x28"
                                                                 "\"}");

    run_test("3.1", "Trailing comma in object", "{\"a\":1,}");
    run_test("3.2", "Trailing comma in array", "[1,2,]");
    run_test("3.3", "Missing comma", "{\"a\":1 \"b\":2}");
    run_test("3.4", "Missing colon", "{\"a\" 1}");
    run_test("3.5", "Empty object", "{}");
    run_test("3.6", "Empty array", "[]");
    run_test("3.7", "Nested empty structures", "[[[[[]]]]]");
    run_test("3.8", "Duplicate keys", "{\"a\":1,\"a\":2}");
    run_test("3.9", "Single quotes", "{'a':1}");
    run_test("3.10", "Unquoted key", "{a:1}");
    run_test("3.11", "Line comment", "{\"a\":1 // comment\n}");
    run_test("3.12", "Block comment", "{\"a\":1 /* c */}");
    run_test("3.13", "Trailing garbage", "{\"a\":1}garbage");
    run_test("3.14", "Leading whitespace", "   {\"a\":1}");
    run_test("3.15", "Trailing whitespace", "{\"a\":1}   ");
    run_test("3.16", "UTF-8 BOM", "\xEF\xBB\xBF"
                                  "{\"a\":1}");
    run_test("3.17", "Only whitespace", "   ");
    run_test("3.18", "Empty input", "");
    run_test("3.19a", "Top-level true", "true");
    run_test("3.19b", "Top-level false", "false");
    run_test("3.19c", "Top-level null", "null");
    run_test("3.20", "Mismatched delimiters", "{\"a\":1]");

    for (i = 0; i < sizeof(depths) / sizeof(depths[0]); i++)
    {
        size_t depth = depths[i];
        memset(nested_input, '[', depth);
        memset(nested_input + depth, ']', depth);
        nested_input[depth * 2] = '\0';
        snprintf(label, sizeof(label), "Nested empty arrays: %lu levels", (unsigned long)depth);
        run_test(depth_ids[i], label, nested_input);
    }
    used = 0;
    wide_input[used++] = '{';
    for (i = 0; i < 1000; i++)
    {
        int written = snprintf(wide_input + used, sizeof(wide_input) - used,
                               "%s\"k%lu\":1", i ? "," : "", (unsigned long)i);
        if (written < 0 || (size_t)written >= sizeof(wide_input) - used)
        {
            fputs("Failed to construct wide object\n", stderr);
            return 2;
        }
        used += (size_t)written;
    }
    wide_input[used++] = '}';
    wide_input[used] = '\0';
    run_test("4.4", "Wide object: 1,000 distinct keys", wide_input);

    run_test("5.1a", "Uppercase True", "{\"a\":True}");
    run_test("5.1b", "Uppercase False", "{\"a\":False}");
    run_test("5.1c", "Uppercase Null", "{\"a\":Null}");
    run_test("5.2", "Mixed array", "[1,\"a\",true,null,{}]");
    run_test("5.3", "Object in array", "[{\"a\":1}]");
    run_test("5.4", "Null object value", "{\"a\":null}");
    printf("\nTotal: %u tests | ACCEPTED: %u | REJECTED: %u\n",
           accepted + rejected, accepted, rejected);
    puts("Timing is a single-run CPU measurement; 0.000 ms can mean below timer resolution.");
    return 0; /* Rejected inputs are expected results, not runner failures. */
}
