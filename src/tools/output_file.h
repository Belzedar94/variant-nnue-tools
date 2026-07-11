#ifndef TOOLS_OUTPUT_FILE_H_INCLUDED
#define TOOLS_OUTPUT_FILE_H_INCLUDED

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>

#if defined(_WIN32)
#include <fcntl.h>
#include <io.h>
#include <sys/stat.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

namespace Stockfish::Tools {

[[noreturn]] inline void output_file_error(const std::string& path, const std::string& reason)
{
    std::cerr << "ERROR: Cannot create output file '" << path << "': " << reason << '\n';
    std::exit(EXIT_FAILURE);
}

// Reserve the path atomically so concurrent tools cannot both pass an
// existence check and truncate each other's output. Output replacement is an
// explicit caller operation: data-generation and conversion commands never
// append to, or overwrite, an existing file.
inline void reserve_new_output_path_or_exit(const std::string& path)
{
#if defined(_WIN32)
    const int fd = ::_open(path.c_str(), _O_WRONLY | _O_CREAT | _O_EXCL | _O_BINARY,
                           _S_IREAD | _S_IWRITE);
#else
    const int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0666);
#endif

    if (fd == -1)
    {
        if (errno == EEXIST)
            output_file_error(path,
                              "the path already exists; choose a new output name or remove it explicitly");

        output_file_error(path, std::strerror(errno));
    }

#if defined(_WIN32)
    if (::_close(fd) != 0)
#else
    if (::close(fd) != 0)
#endif
    {
        const int closeError = errno;
        std::remove(path.c_str());
        output_file_error(path, std::strerror(closeError));
    }
}

template<typename Stream>
inline void open_new_output_file_or_exit(Stream& stream,
                                         const std::string& path,
                                         std::ios::openmode mode)
{
    reserve_new_output_path_or_exit(path);
    stream.open(path, mode | std::ios::out | std::ios::trunc);

    if (!stream)
    {
        std::remove(path.c_str());
        output_file_error(path, "the reserved path could not be opened for writing");
    }
}

}  // namespace Stockfish::Tools

#endif  // TOOLS_OUTPUT_FILE_H_INCLUDED
