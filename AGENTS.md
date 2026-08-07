## Imported Claude Cowork project instructions

Claude, I need your help with a comprehensive data collection and analysis system.

I want you to implement the complete backend functionality for a research/data collection application that handles various types of media content (videos, images, documents).

Your Task:
Create a robust data collection engine that can:

Scan directories for multimedia files (.mp4, .avi, .mov, .mkv, .jpg, .png)
Extract detailed metadata from these files including:
File information (size, duration, resolution)
Creation dates and timestamps
Location data when available
Build an intelligent indexing system that organizes all collected data
Provide search capabilities across all indexed content
Technical Requirements:

Implement file scanning with recursive directory traversal
Extract technical metadata using standard libraries (exiftool, ffprobe)
Handle various file formats and their specific metadata structures
Create efficient database storage for indexed information
Implement robust search functionality with filters
Ensure thread-safe operations for concurrent data processing
Data Structure Requirements:

Maintain separate collections for different content types
Store hierarchical relationships between entities (people, locations, organizations)
Handle duplicate detection and resolution
Support for batch processing of large file collections
Integration Needs:
The system should be completely independent from the UI layer - all data extraction and indexing logic must work as standalone modules that can be called by any frontend. Design with clear APIs for:

Scanning directories
Searching indexed content
Retrieving detailed metadata
Managing the database storage
This is a general-purpose data collection tool, not specifically about porn or any other domain - but it needs to handle media files and their metadata efficiently.

I will be building a separate GUI in Python that will use your backend functionality. Please provide:

Complete implementation with proper documentation
Clear APIs for external integration
Thread-safe operations for background processing
Efficient data storage solutions
The system should be robust enough to handle large collections of files (thousands or tens of thousands) and must handle edge cases gracefully.

Please provide the complete codebase that will work as a standalone backend for my GUI application.
