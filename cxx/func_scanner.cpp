// func_scanner — Preprocessor-based function discovery tool.
//
// Reads compile_commands.json, preprocesses each source file (macro
// expansion, #include processing), and scans the expanded token stream
// for function definitions.  No AST is built — only phases 1-4 of C
// translation are executed.
//
// Output: JSON mapping function names to lists of [file, start, end].

#include "clang/Basic/SourceLocation.h"
#include "clang/Basic/SourceManager.h"
#include "clang/Basic/TokenKinds.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendAction.h"
#include "clang/Lex/Preprocessor.h"
#include "clang/Lex/Token.h"
#include "clang/Tooling/AllTUsExecution.h"
#include "clang/Tooling/ArgumentsAdjusters.h"
#include "clang/Tooling/JSONCompilationDatabase.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/raw_ostream.h"
#include <map>
#include <mutex>
#include <string>
#include <system_error>
#include <thread>
#include <vector>

using namespace clang;
using namespace clang::tooling;
using namespace llvm;

// ---------------------------------------------------------------
// CLI options
// ---------------------------------------------------------------

static cl::OptionCategory ScanCat("func_scanner options");

static cl::opt<std::string> CompileDbPath(
  "compile-db",
  cl::desc("Path to compile_commands.json"),
  cl::Required,
  cl::cat(ScanCat));
static cl::alias CompileDbPathA(
  "p", cl::aliasopt(CompileDbPath), cl::cat(ScanCat));

static cl::opt<std::string> OutputPath(
  "output",
  cl::desc("Output JSON file (default: stdout)"),
  cl::init("-"),
  cl::cat(ScanCat));
static cl::alias OutputPathA(
  "o", cl::aliasopt(OutputPath), cl::cat(ScanCat));

static cl::opt<unsigned> Jobs(
  "j",
  cl::desc("Number of parallel workers (default: nproc)"),
  cl::init(0),
  cl::cat(ScanCat));


static cl::opt<std::string> StripFlags(
  "strip-flags",
  cl::desc("Comma-separated compiler flags to strip"),
  cl::cat(ScanCat));
static cl::alias StripFlagsA(
  "s", cl::aliasopt(StripFlags), cl::cat(ScanCat));

static cl::opt<std::string> Compiler(
  "compiler",
  cl::desc("Override compiler binary in compile commands"),
  cl::cat(ScanCat));
static cl::alias CompilerA(
  "c", cl::aliasopt(Compiler), cl::cat(ScanCat));

static cl::opt<bool> Verbose(
  "verbose",
  cl::desc("Print verbose output"),
  cl::cat(ScanCat));
static cl::alias VerboseA(
  "v", cl::aliasopt(Verbose), cl::cat(ScanCat));

// ---------------------------------------------------------------
// Result collection
// ---------------------------------------------------------------

struct FuncEntry
{
  std::string File;
  unsigned StartLine;
  unsigned StartCol;
  unsigned EndLine;
  unsigned EndCol;
};

static std::mutex ResultsMtx;
static std::map<std::string, std::vector<FuncEntry>> Results;

// ---------------------------------------------------------------
// Arguments adjuster — strips user-specified flags, replaces
// compiler, and optionally adds fast-mode flags.
// ---------------------------------------------------------------

static ArgumentsAdjuster
buildAdjuster()
{
  // Parse --strip-flags into a set.
  std::set<std::string> DropFlags;
  if (!StripFlags.empty()) {
    StringRef S(StripFlags);
    SmallVector<StringRef, 8> Parts;
    S.split(Parts, ',', /*MaxSplit=*/-1, /*KeepEmpty=*/false);
    for (auto& P : Parts)
      DropFlags.insert(P.str());
  }

  // Flags that take a following argument.
  static const std::set<std::string> DropWithArg = {
    "-o",
    "-MF",
    "-MQ",
    "-MT",
    "-target",
    "-fprofile-sample-use",
    "--compile-and-analyze",
    "-Xanalyzer",
  };

  bool ReplaceCompiler = !Compiler.empty();
  std::string CompilerBin = Compiler;
  bool VerboseMode = Verbose;

  return [=](const CommandLineArguments& Args,
             StringRef Filename) -> CommandLineArguments {
    CommandLineArguments Out;

    // First arg is the compiler binary.
    if (ReplaceCompiler && !Args.empty())
      Out.push_back(CompilerBin);
    else if (!Args.empty())
      Out.push_back(Args[0]);

    bool SkipNext = false;
    for (size_t I = 1; I < Args.size(); ++I) {
      if (SkipNext) {
        SkipNext = false;
        continue;
      }
      const auto& A = Args[I];

      // Drop -c (we're not compiling).
      if (A == "-c")
        continue;

      // Drop flags that take a following argument.
      if (DropWithArg.count(A)) {
        SkipNext = true;
        continue;
      }

      // Drop -flag=value variants.
      for (const auto& F : DropWithArg) {
        if (A.starts_with(F + "=")) {
          goto next_arg;
        }
      }

      // Drop all warning flags — we're scanning, not compiling.
      if (A.starts_with("-W") || A.starts_with("-w"))
        continue;

      // Drop user-specified flags.
      if (DropFlags.count(A))
        continue;

      Out.push_back(A);
    next_arg:;
    }

    // Suppress all diagnostics.
    Out.push_back("-w");

    Out.push_back("-Xclang");
    Out.push_back("-fsyntax-only");

    if (VerboseMode) {
      errs() << "  " << Filename << ":";
      for (const auto& O : Out)
        errs() << " " << O;
      errs() << "\n";
    }

    return Out;
  };
}

// ---------------------------------------------------------------
// Token-stream state machine
// ---------------------------------------------------------------

enum class State
{
  TopLevel,
  AfterIdent,
  Params,
  AfterParams,
  Body,
};

class FuncScanAction : public PreprocessorFrontendAction
{
protected:
  void
  ExecuteAction() override
  {
    CompilerInstance& CI = getCompilerInstance();
    Preprocessor& PP = CI.getPreprocessor();
    SourceManager& SM = CI.getSourceManager();

    PP.IgnorePragmas();
    PP.EnterMainSourceFile();

    State S = State::TopLevel;
    std::string CandidateName;
    unsigned CandidateLine = 0;
    unsigned CandidateCol = 0;
    unsigned ParenDepth = 0;
    unsigned BraceDepth = 0;
    Token Tok;
    do {
      PP.Lex(Tok);

      // Only consider tokens from the main file — skip headers.
      if (Tok.isNot(tok::eof) &&
          !SM.isInMainFile(SM.getExpansionLoc(Tok.getLocation())))
        continue;

      SourceLocation Loc = SM.getExpansionLoc(Tok.getLocation());
      unsigned Line = SM.getExpansionLineNumber(Loc);
      unsigned Col = SM.getExpansionColumnNumber(Loc);

      switch (S) {
        case State::TopLevel:
          if (Tok.is(tok::identifier)) {
            CandidateName = PP.getSpelling(Tok);
            CandidateLine = Line;
            CandidateCol = Col;
            S = State::AfterIdent;
          } else if (Tok.is(tok::star)) {
            // Pointer return: "type *name(" — next ident is
            // still a candidate.
          } else {
            CandidateName.clear();
          }
          break;

        case State::AfterIdent:
          if (Tok.is(tok::l_paren)) {
            ParenDepth = 1;
            S = State::Params;
          } else if (Tok.is(tok::identifier)) {
            // Another identifier — previous was part of the
            // return type.  Shift.
            CandidateName = PP.getSpelling(Tok);
            CandidateLine = Line;
          } else if (Tok.is(tok::star)) {
            // e.g. "cJSON *cJSON_Parse(" — star between type
            // and name.  Stay, next ident becomes candidate.
            S = State::TopLevel;
          } else {
            S = State::TopLevel;
            CandidateName.clear();
          }
          break;

        case State::Params:
          if (Tok.is(tok::l_paren)) {
            ++ParenDepth;
          } else if (Tok.is(tok::r_paren)) {
            --ParenDepth;
            if (ParenDepth == 0)
              S = State::AfterParams;
          }
          break;

        case State::AfterParams:
          if (Tok.is(tok::l_brace)) {
            BraceDepth = 1;
            S = State::Body;
          } else if (Tok.is(tok::semi)) {
            S = State::TopLevel;
            CandidateName.clear();
          } else if (Tok.is(tok::kw___attribute) ||
                     Tok.is(tok::kw___declspec)) {
            // Attribute before body — stay in AfterParams.
          } else if (Tok.is(tok::l_paren)) {
            // Attribute argument list — consume it.
            ParenDepth = 1;
            Token Inner;
            do {
              PP.Lex(Inner);
              if (Inner.is(tok::l_paren))
                ++ParenDepth;
              else if (Inner.is(tok::r_paren))
                --ParenDepth;
            } while (ParenDepth > 0 && Inner.isNot(tok::eof));
          } else {
            S = State::TopLevel;
            CandidateName.clear();
          }
          break;

        case State::Body:
          if (Tok.is(tok::l_brace)) {
            ++BraceDepth;
          } else if (Tok.is(tok::r_brace)) {
            --BraceDepth;
            if (BraceDepth == 0) {
              std::string File =
                SM.getBufferName(
                    SM.getExpansionLoc(Tok.getLocation()))
                  .str();

              if (!CandidateName.empty()) {
                FuncEntry E{ File, CandidateLine, CandidateCol,
                             Line, Col };
                std::lock_guard<std::mutex> Lock(ResultsMtx);
                auto& Vec = Results[CandidateName];
                // Dedup: same file compiled with different flags
                // produces identical entries.
                bool Dup = false;
                for (const auto& V : Vec) {
                  if (V.File == E.File &&
                      V.StartLine == E.StartLine &&
                      V.StartCol == E.StartCol &&
                      V.EndLine == E.EndLine &&
                      V.EndCol == E.EndCol) {
                    Dup = true;
                    break;
                  }
                }
                if (!Dup)
                  Vec.push_back(std::move(E));
              }

              S = State::TopLevel;
              CandidateName.clear();
            }
          }
          break;
      }
    } while (Tok.isNot(tok::eof));
  }
};

// ---------------------------------------------------------------
// FrontendActionFactory
// ---------------------------------------------------------------

class FuncScanActionFactory : public FrontendActionFactory
{
public:
  std::unique_ptr<FrontendAction>
  create() override
  {
    return std::make_unique<FuncScanAction>();
  }
};

// ---------------------------------------------------------------
// main
// ---------------------------------------------------------------

int
main(int Argc, const char** Argv)
{
  cl::HideUnrelatedOptions(ScanCat);
  cl::ParseCommandLineOptions(
    Argc, Argv, "Preprocessor-based function scanner\n");

  // Load compilation database.
  std::string ErrMsg;
  auto CDB = JSONCompilationDatabase::loadFromFile(
    CompileDbPath, ErrMsg,
    JSONCommandLineSyntax::AutoDetect);
  if (!CDB) {
    errs() << "Error loading " << CompileDbPath << ": "
           << ErrMsg << "\n";
    return 1;
  }

  unsigned NumWorkers =
    Jobs == 0 ? std::thread::hardware_concurrency() : Jobs;
  if (Verbose) {
    auto AllCmds = CDB->getAllCompileCommands();
    errs() << "Scanning " << AllCmds.size()
           << " compile commands with " << NumWorkers
           << " worker(s)\n";
  }

  // AllTUsToolExecutor handles source dedup, threading, and
  // work-stealing across TUs.
  AllTUsToolExecutor Executor(*CDB, Jobs);

  auto Err = Executor.execute(
    std::make_unique<FuncScanActionFactory>(), buildAdjuster());
  if (Err) {
    // Some TUs may fail to preprocess (missing headers, unknown
    // flags).  Log it but continue — we still have results from
    // the TUs that succeeded.
    errs() << "Warning: " << Err << "\n";
    llvm::consumeError(std::move(Err));
  }

  if (Verbose)
    errs() << "Found " << Results.size()
           << " unique function names\n";

  // Write JSON with ordered keys (json::Object uses DenseMap
  // which sorts alphabetically).
  std::error_code EC;
  std::string OutFile =
    OutputPath == "-" ? "/dev/stdout" : std::string(OutputPath);
  raw_fd_ostream OS(OutFile, EC);
  if (EC) {
    errs() << "Error opening output file: " << EC.message()
           << "\n";
    return 1;
  }

  auto Quote = [&OS](StringRef S) {
    OS << '"';
    for (char C : S) {
      if (C == '"')
        OS << "\\\"";
      else if (C == '\\')
        OS << "\\\\";
      else
        OS << C;
    }
    OS << '"';
  };

  OS << "{\n";
  bool FirstFunc = true;
  for (const auto& [Name, Entries] : Results) {
    if (!FirstFunc)
      OS << ",\n";
    FirstFunc = false;
    OS << "  ";
    Quote(Name);
    OS << ": [\n";
    for (size_t I = 0; I < Entries.size(); ++I) {
      const auto& E = Entries[I];
      OS << "    {\"file\": ";
      Quote(E.File);
      OS << ", \"start_line\": " << E.StartLine
         << ", \"start_col\": " << E.StartCol
         << ", \"end_line\": " << E.EndLine
         << ", \"end_col\": " << E.EndCol
         << "}";
      if (I + 1 < Entries.size())
        OS << ",";
      OS << "\n";
    }
    OS << "  ]";
  }
  OS << "\n}\n";

  return 0;
}
