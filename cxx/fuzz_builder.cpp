#include <clang/AST/RecursiveASTVisitor.h>
#include <clang/ASTMatchers/ASTMatchFinder.h>
#include <clang/ASTMatchers/ASTMatchers.h>
#include "clang/Frontend/ASTUnit.h"
#include <clang/Frontend/FrontendActions.h>
#include <clang/Tooling/CommonOptionsParser.h>
#include <clang/Tooling/CompilationDatabase.h>
#include <clang/Tooling/Tooling.h>
#include "llvm/ADT/StringRef.h"
#include <llvm/Support/JSON.h>
#include <llvm/Support/raw_ostream.h>
#include <memory>
#include <string>
#include <string_view>
#include <system_error>

using namespace clang;
using namespace clang::ast_matchers;
using namespace clang::tooling;
using namespace llvm;

// TypeResolver class
class TypeResolver : public MatchFinder::MatchCallback
{
public:
  TypeResolver(std::vector<std::string>& resolved_types, StringRef func_name)
    : resolved_types(resolved_types)
    , func_name(func_name)
  {
  }

  virtual void run(const MatchFinder::MatchResult& result) override
  {
    const FunctionDecl* func_decl =
      result.Nodes.getNodeAs<FunctionDecl>("funcDecl");
    if (func_decl && func_decl->getName() == func_name) {
      resolve_function_types(func_decl);
    }
  }

private:
  std::vector<std::string>& resolved_types;
  std::string func_name;

  void resolve_function_types(const FunctionDecl* func_decl)
  {
    // Resolve return type
    resolve_type(func_decl->getReturnType());

    // Resolve parameter types
    for (const ParmVarDecl* parm_var_decl : func_decl->parameters()) {
      resolve_type(parm_var_decl->getType());
    }
  }

  void resolve_type(QualType type)
  {
    if (type->isPointerType() || type->isReferenceType()) {
      resolve_type(type->getPointeeType());
    } else if (type->isRecordType()) {
      const RecordDecl* record_decl = type->getAsRecordDecl();
      if (record_decl && record_decl->getDefinition()) {
        std::string type_source = get_source_text(record_decl->getDefinition());
        if (std::find(resolved_types.begin(),
                      resolved_types.end(),
                      type_source) == resolved_types.end()) {
          resolved_types.push_back(type_source);
          resolve_decl_types(record_decl->getDefinition());
        }
      }
    }
  }

  void resolve_decl_types(const Decl* decl)
  {
    if (const RecordDecl* record_decl = dyn_cast<RecordDecl>(decl)) {
      for (const FieldDecl* field_decl : record_decl->fields()) {
        resolve_type(field_decl->getType());
      }
    }
  }

  std::string get_source_text(const Decl* decl)
  {
    SourceManager& sm = decl->getASTContext().getSourceManager();
    SourceLocation start_loc = decl->getBeginLoc();
    SourceLocation end_loc = decl->getEndLoc();
    return std::string(sm.getCharacterData(start_loc),
                       sm.getCharacterData(end_loc) -
                         sm.getCharacterData(start_loc) + 1);
  }
};

class FindFunctionVisitor
  : public RecursiveASTVisitor<FindFunctionVisitor> {
public:
  bool visit_function_decl(FunctionDecl* func_decl)
  {
    StringRef current_function_name = func_decl->getName();

    if (current_function_name == target_function_name) {
      SourceLocation location = func_decl->getLocation();
      SourceManager& source_manager =
        func_decl->getASTContext().getSourceManager();

      if (location.isValid() && location.isFileID()) {
        PresumedLoc presumed_loc = source_manager.getPresumedLoc(location);

        if (presumed_loc.isValid()) {
          file_path = presumed_loc.getFilename();
          return false; // Stop traversal
        }
      }
    }

    return true; // Continue traversal
  }

private:
  // FIXME: assign values to these members
  std::string target_function_name;
  std::string file_path;
};

class FindFunctionConsumer : public clang::ASTConsumer {
public:
  void HandleTranslationUnit(clang::ASTContext &context) override {
    // Traversing the translation unit decl via a RecursiveASTVisitor
    // will visit all nodes in the AST.
    TranslationUnitDecl* decl = context.getTranslationUnitDecl();

    // Perform a pre-order traversal of the AST
    visitor.TraverseDecl(decl);
  }
private:
  // A RecursiveASTVisitor implementation.
  FindFunctionVisitor visitor;
};

// FindFunctionAction class
class FindFunctionAction : public ASTFrontendAction
{
public:
  std::string file_path;
  std::string target_function_name;

  FindFunctionAction(std::string_view target_function_name)
    : target_function_name(std::move(target_function_name))
  {
  }

  std::unique_ptr<ASTConsumer> CreateASTConsumer(CompilerInstance& CI,
                                                 StringRef file) override
  {
    return std::make_unique<FindFunctionConsumer>();
  }
};

llvm::Expected<std::string>
find_function_file(std::string_view function_name,
                   CompilationDatabase& compilation_database)
{
  // Iterate over the source files in the compilation database
  for (const CompileCommand& command :
       compilation_database.getAllCompileCommands()) {
    std::string file_path = command.Filename;

    // Create a ClangTool instance
    ClangTool tool(compilation_database, { file_path });

    FindFunctionAction action(function_name);
    std::unique_ptr<FrontendActionFactory> factory =
      newFrontendActionFactory(&action);

    int result = tool.run(factory.get());
    if (result == 0 && !action.file_path.empty()) {
      return action.file_path;
    }
  }

  return llvm::make_error<StringError>(
    "Could not find source file for function", inconvertibleErrorCode());
}

int
main(int argc, const char** argv)
{
  std::error_code ec;
  CompilationDatabase* cc_ptr;
  std::vector<std::string> source_paths;

  // Parse command-line options
  static cl::OptionCategory fuzz_tool_options("Fuzz Builder options");
  const cl::opt<std::string> function_name("function",
                                           cl::Required,
                                           cl::desc("Name of the function"),
                                           cl::cat(fuzz_tool_options));
  const cl::opt<std::string> triple(
    "triple", cl::desc("Target triple"), cl::cat(fuzz_tool_options));
  const cl::opt<bool> verbose(
    "verbose", cl::desc("Print verbose output"), cl::cat(fuzz_tool_options));
  llvm::Expected<CommonOptionsParser> args =
    CommonOptionsParser::create(argc, argv, fuzz_tool_options);
  if (!args) {
    errs() << args.takeError();
    ec = inconvertibleErrorCode();
    return ec.value();
  }

  // Print the args
  outs() << "FunctionName: " << function_name.getValue() << "\n";
  outs() << "Triple: " << triple.getValue() << "\n";
  outs() << "Verbose: " << verbose << "\n";

  // Retrieve the compilation database and source files to process
  cc_ptr = &args->getCompilations();
  // source_paths = args->getSourcePathList();

  // ClangTool tool(*cc_ptr, source_paths);

  // print all compile commands and exit early
  // if (verbose) {
  //     for (const CompileCommand& command : cdb_ptr->getAllCompileCommands())
  //     {
  //         outs() << "Command: " << command.CommandLine[0] << "\n";
  //         for (const std::string& arg : command.CommandLine) {
  //             outs() << arg << " ";
  //         }
  //         outs() << "\n\n";
  //     }
  //     return 0;
  // }

  // Set the target triple
  //   if (!triple.empty()) {
  //       outs() << "Setting target triple\n";
  //       CompilerInvocation* invocation =
  //       cdb_ptr->getAllCompileCommands()[0].CommandLine;
  //       invocation->setTarget(TargetInfo::CreateTargetInfo(invocation->getDiagnostics(),
  //       invocation->TargetOpts));
  //   }

  // Create an index for parsing the source files
  //   CXIndex index = verbose ? clang_createIndex(0, 1) : clang_createIndex(0, 0);
  //   outs() << "Created index\n";

  // Find the source file given the function name
  llvm::Expected<std::string> file_path =
    find_function_file(function_name.getValue(), *cc_ptr);
  if (!file_path) {
    errs() << file_path.takeError();
    ec = inconvertibleErrorCode();
    return ec.value();
  }
  outs() << "Found source file: " << file_path.get() << "\n";

  ClangTool tool(*cc_ptr, { file_path.get() });
  outs() << "Created ClangTool\n";

  // Make an AST for the source file
  std::vector<std::unique_ptr<ASTUnit>> ast_units;
  tool2.buildASTs(ast_units);
  // If size != 1, then there was an error
  if (ast_units.size() != 1) {
    errs() << "Error building AST for source file\n";
    ec = inconvertibleErrorCode();
    return ec.value();
  }
  outs() << "Built AST for source file\n";

  std::vector<std::string> resolved_types;
  TypeResolver resolver(resolved_types, function_name.getValue());
  MatchFinder finder;
  finder.addMatcher(functionDecl(), &resolver);
  tool.run(newFrontendActionFactory(&finder).get());
  outs() << "Resolved types\n";

  raw_fd_ostream out_file("resolved_types.txt", ec, sys::fs::OF_None);
  if (ec) {
    errs() << "Error opening output file: " << ec.message() << "\n";
    return ec.value();
  }

  for (const std::string& type_source : resolved_types) {
    out_file << type_source << "\n\n";
  }

  return 0;
}