"""
The Staff Area View mixin renders all the staff-specific information used to
determine the flow of the problem.
"""
import copy
from functools import wraps
import logging

from xblock.core import XBlock
from openassessment.assessment.errors import (
    PeerAssessmentInternalError,
)
from openassessment.workflow.errors import (
    AssessmentWorkflowError, AssessmentWorkflowInternalError
)
from openassessment.assessment.errors.ai import AIError
from openassessment.xblock.resolve_dates import DISTANT_PAST, DISTANT_FUTURE
from openassessment.xblock.data_conversion import (
    create_rubric_dict, convert_training_examples_list_to_dict, create_submission_dict
)
from submissions import api as submission_api
from openassessment.assessment.api import peer as peer_api
from openassessment.assessment.api import self as self_api
from openassessment.assessment.api import ai as ai_api
from openassessment.fileupload import api as file_api
from openassessment.workflow import api as workflow_api
from openassessment.assessment.api import staff as staff_api
from openassessment.fileupload import exceptions as file_exceptions


logger = logging.getLogger(__name__)


def require_global_admin(error_key):
    """
    Method decorator to restrict access to an XBlock handler
    to only global staff.

    Args:
        error_key (str): The key to the error message to display to the user
        if they do not have sufficient permissions.

    Returns:
        Decorated function

    """
    def _decorator(func):  # pylint: disable=C0111
        @wraps(func)
        def _wrapped(xblock, *args, **kwargs):  # pylint: disable=C0111
            permission_errors = {
                "SCHEDULE_TRAINING": xblock._(u"You do not have permission to schedule training"),
                "RESCHEDULE_TASKS": xblock._(u"You do not have permission to reschedule tasks."),
            }
            if not xblock.is_admin or xblock.in_studio_preview:
                return {'success': False, 'msg': permission_errors[error_key]}
            else:
                return func(xblock, *args, **kwargs)
        return _wrapped
    return _decorator


def require_course_staff(error_key, with_json_handler=False):
    """
    Method decorator to restrict access to an XBlock render
    method to only course staff.

    Args:
        error_key (str): The key for the error message to display to the
            user if they do not have sufficient permissions.

    Returns:
        decorated function

    """
    def _decorator(func):  # pylint: disable=C0111
        @wraps(func)
        def _wrapped(xblock, *args, **kwargs):  # pylint: disable=C0111
            permission_errors = {
                "STAFF_AREA": xblock._(u"You do not have permission to access the ORA staff area"),
                "STUDENT_INFO": xblock._(u"You do not have permission to access ORA learner information."),
                "STUDENT_GRADE": xblock._(u"You do not have permission to access ORA staff grading."),
            }

            if not xblock.is_course_staff and with_json_handler:
                return {"success": False, "msg": permission_errors[error_key]}
            elif not xblock.is_course_staff or xblock.in_studio_preview:
                return xblock.render_error(permission_errors[error_key])
            else:
                return func(xblock, *args, **kwargs)
        return _wrapped
    return _decorator


class StaffAreaMixin(object):
    """
    Display debug information to course and global staff.
    """

    @XBlock.handler
    @require_course_staff("STAFF_AREA")
    def render_staff_area(self, data, suffix=''):  # pylint: disable=W0613
        """
        Template context dictionary for course staff debug panel.

        Returns:
            dict: The template context specific to the course staff debug panel.

        """
        path, context = self.get_staff_path_and_context()
        return self.render_assessment(path, context)

    def get_staff_path_and_context(self):
        """
        Gets the path and context for the staff section of the ORA XBlock.
        """
        context = {}
        path = 'openassessmentblock/staff_area/oa_staff_area.html'

        student_item = self.get_student_item_dict()

        # We need to display the new-style locations in the course staff
        # info, even if we're using old-style locations internally,
        # so course staff can use the locations to delete student state.
        context['item_id'] = student_item["item_id"]

        # Calculate how many students are in each step of the workflow
        status_counts, num_submissions = self.get_workflow_status_counts()
        context['status_counts'] = status_counts
        context['num_submissions'] = num_submissions

        # Show the schedule training button if example based assessment is
        # configured, and the current user has admin privileges.
        example_based_assessment = self.get_assessment_module('example-based-assessment')
        display_ai_staff_info = (
            self.is_admin and
            bool(example_based_assessment) and
            not self.in_studio_preview
        )
        context['display_schedule_training'] = display_ai_staff_info
        context['display_reschedule_unfinished_tasks'] = display_ai_staff_info
        if display_ai_staff_info:
            context['classifierset'] = ai_api.get_classifier_set_info(
                create_rubric_dict(self.prompts, self.rubric_criteria_with_labels),
                example_based_assessment['algorithm_id'],
                student_item['course_id'],
                student_item['item_id']
            )

        # Include Latex setting
        context['allow_latex'] = self.allow_latex

        # Include release/due dates for each step in the problem
        context['step_dates'] = list()
        for step in ['submission'] + self.assessment_steps:

            if step == 'example-based-assessment':
                continue

            # Get the dates as a student would see them
            __, __, start_date, due_date = self.is_closed(step=step, course_staff=False)

            context['step_dates'].append({
                'step': step,
                'start': start_date if start_date > DISTANT_PAST else None,
                'due': due_date if due_date < DISTANT_FUTURE else None,
            })

        # Include whether or not staff grading step is enabled.
        staff_assessment_required = "staff-assessment" in self.assessment_steps
        context['staff_assessment_required'] = staff_assessment_required
        if staff_assessment_required:
            grading_stats = staff_api.get_staff_grading_statistics(
                student_item["course_id"], student_item["item_id"]
            )
            context['staff_assessment_ungraded'] = grading_stats['ungraded']
            context['staff_assessment_in_progress'] = grading_stats['in-progress']

        return path, context

    @XBlock.json_handler
    @require_global_admin("SCHEDULE_TRAINING")
    def schedule_training(self, data, suffix=''):  # pylint: disable=W0613
        """
        Schedule a new training task for example-based grading.
        """
        assessment = self.get_assessment_module('example-based-assessment')
        student_item_dict = self.get_student_item_dict()

        if assessment:
            examples = assessment["examples"]
            try:
                workflow_uuid = ai_api.train_classifiers(
                    create_rubric_dict(self.prompts, self.rubric_criteria_with_labels),
                    convert_training_examples_list_to_dict(examples),
                    student_item_dict.get('course_id'),
                    student_item_dict.get('item_id'),
                    assessment["algorithm_id"]
                )
                return {
                    'success': True,
                    'workflow_uuid': workflow_uuid,
                    'msg': self._(u"Training scheduled with new Workflow UUID: {uuid}".format(uuid=workflow_uuid))
                }
            except AIError as err:
                return {
                    'success': False,
                    'msg': self._(u"An error occurred scheduling classifier training: {error}".format(error=err))
                }

        else:
            return {
                'success': False,
                'msg': self._(u"Example Based Assessment is not configured for this location.")
            }

    @XBlock.handler
    @require_course_staff("STUDENT_INFO")
    def render_student_info(self, data, suffix=''):  # pylint: disable=W0613
        """
        Renders all relative information for a specific student's workflow.

        Given a student's username, we can render a staff-only section of the page
        with submissions and assessments specific to the student.

        Must be course staff to render this view.

        """
        try:
            student_username = data.params.get('student_username', '')
            expanded_view = data.params.get('expanded_view', [])
            path, context = self.get_student_info_path_and_context(
                student_username,
                expanded_view=expanded_view
            )
            return self.render_assessment(path, context)

        except PeerAssessmentInternalError:
            return self.render_error(self._(u"Error getting learner information."))

    @XBlock.handler
    @require_course_staff("STUDENT_GRADE")
    def render_staff_grade_form(self, data, suffix=''):  # pylint: disable=W0613
        """
        Renders a form to staff-grade the next available learner submission.

        Must be course staff to render this view.
        """
        try:
            student_item_dict = self.get_student_item_dict()
            course_id = student_item_dict.get('course_id')
            item_id = student_item_dict.get('item_id')
            staff_id = student_item_dict['student_id']

            # Note that this will check out a submission for grading by the specified staff member.
            # If no submissions are available for graidng, will reutrn None.
            submission_to_assess = staff_api.get_submission_to_assess(course_id, item_id, staff_id)

            if submission_to_assess is not None:
                submission = submission_api.get_submission_and_student(submission_to_assess['uuid'])
                if submission:
                    anonymous_student_id = submission['student_item']['student_id']
                    submission_context = self.get_student_submission_context(
                        self.get_username(anonymous_student_id), submission
                    )
                    path = 'openassessmentblock/staff_area/oa_staff_grade_learners_assessment.html'
                    return self.render_assessment(path, submission_context)
                else:
                    return self.render_error(self._(u"Error loading the checked out learner response."))
            else:
                return self.render_error(self._(u"No other learner responses are available for grading at this time."))

        except PeerAssessmentInternalError:
            return self.render_error(self._(u"Error getting staff grade information."))

    def get_student_submission_context(self, student_username, submission):
        """
        Get a context dict for rendering a student submission and associated rubric (for staff grading).
        Includes submission (populating submitted file information if relevant), rubric_criteria,
        and student_username.

        Args:
            student_username (unicode): The username of the student to report.
            submission (object): A submission, as returned by the submission_api.

        Returns:
            A context dict for rendering a student submission and associated rubric (for staff grading).
        """
        if submission and 'file_key' in submission.get('answer', {}):
            file_key = submission['answer']['file_key']

            try:
                submission['file_url'] = file_api.get_download_url(file_key)
            except file_exceptions.FileUploadError:
                # Log the error, but do not prevent the rest of the student info
                # from being displayed.
                msg = (
                    u"Could not retrieve image URL for staff debug page.  "
                    u"The learner username is '{student_username}', and the file key is {file_key}"
                ).format(student_username=student_username, file_key=file_key)
                logger.exception(msg)

        context = {
            'submission': create_submission_dict(submission, self.prompts) if submission else None,
            'rubric_criteria': copy.deepcopy(self.rubric_criteria_with_labels),
            'student_username': student_username,
        }

        return context

    def get_student_info_path_and_context(self, student_username, expanded_view=None):
        """
        Get the proper path and context for rendering the student info
        section of the staff area.

        Args:
            student_username (unicode): The username of the student to report.
            expanded_view (str): An optional view to be shown initially expanded.
                The default is None meaning that all views are shown collapsed.
        """
        anonymous_user_id = None
        student_item = None
        submissions = None
        submission = None
        submission_uuid = None

        if student_username:
            anonymous_user_id = self.get_anonymous_user_id(student_username, self.course_id)
            student_item = self.get_student_item_dict(anonymous_user_id=anonymous_user_id)

        if anonymous_user_id:
            # If there is a submission available for the requested student, present
            # it. If not, there will be no other information to collect.
            submissions = submission_api.get_submissions(student_item, 1)

        if submissions:
            submission = submissions[0]
            submission_uuid = submission['uuid']

        context = self.get_student_submission_context(student_username, submission)

        assessment_steps = self.assessment_steps

        example_based_assessment = None
        self_assessment = None
        peer_assessments = None
        submitted_assessments = None

        if "peer-assessment" in assessment_steps:
            peer_assessments = peer_api.get_assessments(submission_uuid)
            submitted_assessments = peer_api.get_submitted_assessments(submission_uuid, scored_only=False)

        if "self-assessment" in assessment_steps:
            self_assessment = self_api.get_assessment(submission_uuid)

        if "example-based-assessment" in assessment_steps:
            example_based_assessment = ai_api.get_latest_assessment(submission_uuid)

        workflow = self.get_workflow_info(submission_uuid=submission_uuid)

        workflow_cancellation = self.get_workflow_cancellation_info(submission_uuid)

        context.update({
            'score': workflow.get('score'),
            'workflow_status': workflow.get('status'),
            'workflow_cancellation': workflow_cancellation,
            'peer_assessments': peer_assessments,
            'submitted_assessments': submitted_assessments,
            'self_assessment': self_assessment,
            'example_based_assessment': example_based_assessment,
            'expanded_view': expanded_view,
        })

        if peer_assessments or self_assessment or example_based_assessment:
            max_scores = peer_api.get_rubric_max_scores(submission_uuid)
            for criterion in context["rubric_criteria"]:
                criterion["total_value"] = max_scores[criterion["name"]]

        path = 'openassessmentblock/staff_area/oa_student_info.html'
        return path, context

    @XBlock.json_handler
    @require_global_admin("RESCHEDULE_TASKS")
    def reschedule_unfinished_tasks(self, data, suffix=''):  # pylint: disable=W0613
        """
        Wrapper which invokes the API call for rescheduling grading tasks.

        Checks that the requester is an administrator that is not in studio-preview mode,
        and that the api-call returns without error.  If it returns with an error, (any
        exception), the appropriate JSON serializable dictionary with success conditions
        is passed back.

        Args:
            data (not used)
            suffix (not used)

        Return:
            Json serilaizable dict with the following elements:
                'success': (bool) Indicates whether or not the tasks were rescheduled successfully
                'msg': The response to the server (could be error message or success message)
        """
        # Identifies the course and item that will need to be re-run
        student_item_dict = self.get_student_item_dict()
        course_id = student_item_dict.get('course_id')
        item_id = student_item_dict.get('item_id')

        try:
            # Note that we only want to recschdule grading tasks, but maintain the potential functionallity
            # within the API to also reschedule training tasks.
            ai_api.reschedule_unfinished_tasks(course_id=course_id, item_id=item_id, task_type=u"grade")
            return {
                'success': True,
                'msg': self._(u"All AI tasks associated with this item have been rescheduled successfully.")
            }
        except AIError as ex:
            return {
                'success': False,
                'msg': self._(u"An error occurred while rescheduling tasks: {}".format(ex))
            }

    @XBlock.json_handler
    @require_course_staff("STUDENT_INFO", with_json_handler=True)
    def cancel_submission(self, data, suffix=''):
        """
            This will cancel the assessment + peer workflow for the particular submission.

            Args:
                data (dict): Data contain two attributes: submission_uuid and
                    comments. submission_uuid is id of submission which is to be
                    removed from the grading pool. Comments is the reason given
                    by the user.

                suffix (not used)

            Return:
                Json serializable dict with the following elements:
                    'success': (bool) Indicates whether or not the workflow cancelled successfully.
                    'msg': The response (could be error message or success message).
        """
        submission_uuid = data.get('submission_uuid')
        comments = data.get('comments')

        if not comments:
            return {"success": False, "msg": self._(u'Please enter valid reason to remove the submission.')}

        student_item_dict = self.get_student_item_dict()
        try:
            assessment_requirements = self.workflow_requirements()
            # Cancel the related workflow.
            workflow_api.cancel_workflow(
                submission_uuid=submission_uuid, comments=comments,
                cancelled_by_id=student_item_dict['student_id'],
                assessment_requirements=assessment_requirements
            )
            return {
                "success": True,
                'msg': self._(
                    u"The learner submission has been removed from peer assessment. "
                    u"The learner receives a grade of zero unless you delete "
                    u"the learner's state for the problem to allow them to "
                    u"resubmit a response."
                )
            }
        except (
                AssessmentWorkflowError,
                AssessmentWorkflowInternalError
        ) as ex:
            msg = ex.message
            logger.exception(msg)
            return {"success": False, 'msg': msg}
