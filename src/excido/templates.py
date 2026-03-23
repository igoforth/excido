from string import Template

fuzz_template = Template(
    """$includes

$stubs

$types

#ifndef __AFL_FUZZ_TESTCASE_LEN
  int fuzz_len;
  #ifndef _UNISTD_H
  extern int read(int, void *, unsigned int);
  #endif
  #define __AFL_FUZZ_TESTCASE_LEN fuzz_len
  unsigned char fuzz_buf[$buflen]; /* estimate: adjust based on which parameters you fuzz */
  #define __AFL_FUZZ_TESTCASE_BUF fuzz_buf
  #define __AFL_FUZZ_INIT() void sync(void);
  #define __AFL_LOOP(x) ((fuzz_len = read(0, fuzz_buf, sizeof(fuzz_buf))) > 0 ? 1 : 0)
  #define __AFL_INIT() sync()
#endif

__AFL_FUZZ_INIT();

int main() {
  $callers
  $declarations

#ifdef __AFL_HAVE_MANUAL_CONTROL
  __AFL_INIT();
#endif

  unsigned char *buf = __AFL_FUZZ_TESTCASE_BUF;

  while (__AFL_LOOP(10000)) {

    int len = __AFL_FUZZ_TESTCASE_LEN;

    if (len < $minlen) continue;  /* estimate: you may only fuzz a subset of parameters */

    $setup

    $call

    $reset

  }

  return 0;

}"""
)

# Coccinelle semantic patch: find callers (report mode with OCaml scripting)
# $function is substituted at runtime
cocci_report_template = Template(
    """\
virtual report

@caller@
identifier fn;
expression list ES;
position p1, p2;
@@

fn@p1(...) {
  ...
  $function@p2(ES)
  ...
}

@script:ocaml depends on report@
fn << caller.fn;
p1 << caller.p1;
p2 << caller.p2;
@@

Printf.printf "CALLER:%s:%s:%d\\n"
  fn (List.hd p2).file (List.hd p2).line
"""
)

# Coccinelle semantic patch: show call site context (context mode)
# $function is substituted at runtime
cocci_context_template = Template(
    """\
virtual context

@caller@
identifier fn;
expression list ES;
@@

fn(...) {
  ...
* $function(ES)
  ...
}
"""
)
