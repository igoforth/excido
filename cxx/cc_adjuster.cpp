#include "clang/Tooling/ArgumentsAdjusters.h"
#include "clang/Tooling/CompilationDatabase.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/Path.h"
#include <string>

using namespace std;
using namespace clang;
using namespace clang::tooling;
using namespace llvm;
using namespace llvm::cl;
using namespace llvm::sys::fs;
using namespace llvm::sys::path;

// Helper function to replace a substring in a string
string
replace_string(const string& str, const string& from, const std::string& to)
{
  string result = str;
  size_t pos = 0;
  while ((pos = result.find(from, pos)) != string::npos) {
    result.replace(pos, from.length(), to);
    pos += to.length();
  }
  return result;
}

unique_ptr<CompilationDatabase>
load_from_file(const string& file_path, string& error_message)
{
  // Extract the directory from the file path
  string directory = parent_path(file_path).str();

  // Load the compilation database from the directory
  unique_ptr<CompilationDatabase> database =
    CompilationDatabase::loadFromDirectory(directory, error_message);

  // If loading failed, print the error message and return nullptr
  if (!database) {
    return nullptr;
  }

  // Return the loaded compilation database
  return database;
}

// Custom argument adjuster for fixing compile commands
class CompilerFixer : public ArgumentsAdjuster
{
public:
  CompilerFixer(string orig_src, string targ_src, string c, string cxx)
    : orig_src(std::move(orig_src))
    , targ_src(std::move(targ_src))
    , c(std::move(c))
    , cxx(std::move(cxx))
  {
  }

  CommandLineArguments adjust(const CommandLineArguments& args)
  {
    CommandLineArguments adjusted_args;
    for (const auto& arg : args) {
      string adjusted_arg = arg;
      if (arg.starts_with("cc")) {
        adjusted_arg = c + arg.substr(2);
      } else if (arg.starts_with("c++")) {
        adjusted_arg = cxx + arg.substr(3);
      }
      adjusted_arg = replace_string(adjusted_arg, orig_src, targ_src);
      adjusted_args.push_back(adjusted_arg);
    }
    return adjusted_args;
  }

private:
  string orig_src;
  string targ_src;
  string c;
  string cxx;
};

// Custom argument adjuster for fixing directory and file paths
class PathFixer : public ArgumentsAdjuster
{
public:
  PathFixer(string orig_src, string targ_src)
    : orig_src(std::move(orig_src))
    , targ_src(std::move(targ_src))
  {
  }

  CommandLineArguments adjust(const CommandLineArguments& args)
  {
    CommandLineArguments adjusted_args;
    for (const auto& arg : args) {
      string adjusted_arg = replace_string(arg, orig_src, targ_src);
      adjusted_args.push_back(adjusted_arg);
    }
    return adjusted_args;
  }

private:
  string orig_src;
  string targ_src;
};

// Custom argument adjuster for adding or changing target triple
class TripleFixer : public ArgumentsAdjuster
{
public:
  TripleFixer(string triple)
    : triple(std::move(triple))
  {
  }

  CommandLineArguments adjust(const CommandLineArguments& args)
  {
    CommandLineArguments adjusted_args;
    bool next = false;
    bool found = false;
    for (const auto& arg : args) {
      // remove existing target triple
      if (arg == "-target") {
        next = true;
        found = true;
        continue;
      }
      // insert new target triple
      if (next) {
        adjusted_args.push_back(triple);
        next = false;
        continue;
      }
      // add other arguments
      else {
        adjusted_args.push_back(arg);
      }
    }
    // insert new target triple after compiler if not found
    if (!found) {
      adjusted_args.insert(adjusted_args.begin() + 1, {"-target", triple});
    }
    return adjusted_args;
  }

private:
  string triple;
};

int
main(int argc, const char** argv)
{
  error_code ec;
  string error_message;

  opt<std::string> input_file(
    Positional, desc("<input file>"), Required);
  opt<std::string> output_file(
    "o", desc("Specify output filename"), value_desc("filename"), Required);
  opt<std::string> orig_src(
    "orig-src", desc("Path to original source"), Required);
  opt<std::string> targ_src(
    "targ-src", llvm::cl::desc("Path to target source"), Required);
  opt<std::string> c(
    "c", desc("Path to clang"), Required);
  opt<std::string> cxx(
    "cxx", desc("Path to clang++"), Required);

  ParseCommandLineOptions(argc, argv);

  // Load the compile commands from the input file
  auto expected_database = load_from_file(input_file, error_message);
  if (!expected_database) {
    ec = make_error_code(llvm::errc::no_such_file_or_directory);
    llvm::errs() << "Error loading compile commands: " << error_message << "\n";
    return ec.value();
  }

  // Create the argument adjusters
  CompilerFixer com_fixer(orig_src, targ_src, c, cxx);
  PathFixer dir_fixer(orig_src, targ_src);
  PathFixer fil_fixer(orig_src, targ_src);

  // Adjust the compile commands
  vector<CompileCommand> adjusted_commands;
  for (const auto& command : expected_database->getAllCompileCommands()) {
    auto adjusted_command = command;
    adjusted_command.CommandLine = com_fixer.adjust(command.CommandLine);
    adjusted_command.Directory = dir_fixer.adjust({ command.Directory })[0];
    adjusted_command.Filename = fil_fixer.adjust({ command.Filename })[0];
    adjusted_commands.push_back(adjusted_command);
  }

  // Write the adjusted compile commands to the output file
  raw_fd_ostream os(output_file, ec, OF_Text);
  if (ec) {
    errs() << "Error opening output file: " << ec.message() << "\n";
    return ec.value();
  }

  llvm::json::Value commands(llvm::json::Array{});
  for (const auto& command : adjusted_commands) {
    llvm::json::Object obj{
      { "directory", command.Directory },
      { "command", llvm::join(command.CommandLine, " ") },
      { "file", command.Filename },
    };
    commands.getAsArray()->push_back(std::move(obj));
  }
  os << formatv("{0:2}", commands);

  return 0;
}